"""消费端的取数与脏数据处理测试，用假客户端代替 Redis。"""

import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from stream.consumer import _replay_bucket, ensure_group, read_batch, run_window

TZ = timezone(timedelta(hours=8))
WINDOW_START = datetime(2026, 10, 20, 9, 30, 0, tzinfo=TZ)


class FakeRedis:
    def __init__(self, batches=None, group_error=None):
        self.batches = list(batches or [])
        self.group_error = group_error
        self.created = []
        self.acked = []

    def xreadgroup(self, group, consumer, streams, count=None, block=None):
        return self.batches.pop(0) if self.batches else None

    def xgroup_create(self, stream, group, id=None, mkstream=False):
        if self.group_error:
            raise self.group_error
        self.created.append((stream, group, id))

    def xack(self, stream, group, *ids):
        self.acked.extend(ids)


def test_read_batch_parses_payload():
    payload = {"station_id": "A01", "line_id": "L1", "passengers": 2,
               "event_time": "2026-10-20T09:30:00.100+08:00"}
    client = FakeRedis([[("gate_events", [("1-0", {"data": json.dumps(payload)})])]])
    events, ids = read_batch(client, "worker-1")
    assert events == [payload]
    assert ids == ["1-0"]


def test_read_batch_turns_broken_payload_into_empty_event():
    messages = [("1-0", {"data": "{不是 json"}), ("2-0", {})]
    events, ids = read_batch(FakeRedis([[("gate_events", messages)]]), "worker-1")
    # 两条坏消息都要留下，交给 aggregate 记非法；跳过会连同 ACK 一起抹掉
    assert events == [{}, {}]
    assert ids == ["1-0", "2-0"]


def test_read_batch_handles_no_data():
    events, ids = read_batch(FakeRedis([]), "worker-1")
    assert events == [] and ids == []


def test_ensure_group_ignores_busygroup():
    from redis.exceptions import ResponseError

    client = FakeRedis(group_error=ResponseError("BUSYGROUP Consumer Group name already exists"))
    ensure_group(client)  # 不应抛出


def test_ensure_group_creates_from_zero():
    client = FakeRedis()
    ensure_group(client)
    assert client.created == [("gate_events", "metric_builder", "0")]


# ---- persist_window 测试（指标口径 §4、测试方案 TC-19/TC-21）----

def _sample_result():
    """典型聚合结果，供 persist_window 测试复用。"""
    return {
        "total": 6,
        "stations": [
            {"station_id": "A01", "line_id": "L1", "passengers": 5},
            {"station_id": "B01", "line_id": "L2", "passengers": 1},
        ],
        "late_count": 1,
        "invalid_count": 0,
        "late_by_station": {"A01": 1},
    }


# 注意：persist_window 里是延迟 import（`from api.models import ...`），
# 所以 patch 目标必须是 api.models.*，而不是 stream.consumer.*。
_PATCH_WINDOW = "api.models.MetricWindow"
_PATCH_QUALITY = "api.models.MetricQuality"


def _patched_models(window=None, quality=None):
    """同时替换两张表，避免测试意外访问真实数据库。"""
    window = window or MagicMock()
    quality = quality or MagicMock()
    return patch(_PATCH_WINDOW, window), patch(_PATCH_QUALITY, quality)


def test_persist_window_uses_update_conflicts():
    """同键要覆盖而不是丢弃：重试时后到的完整聚合不能输给第一次的残值。"""
    import stream.consumer as consumer
    consumer._django_ready = True
    consumer._persist_warned = False

    fake_model, fake_quality = MagicMock(), MagicMock()
    p1, p2 = _patched_models(fake_model, fake_quality)
    with p1, p2:
        assert consumer.persist_window(WINDOW_START, _sample_result()) is True
        fake_model.objects.bulk_create.assert_called_once()
        _, kwargs = fake_model.objects.bulk_create.call_args
        assert kwargs.get("update_conflicts") is True
        assert kwargs.get("unique_fields") == ["ts", "station_id"]
        assert set(kwargs.get("update_fields")) == {
            "passengers", "in_passengers", "out_passengers", "late_count"
        }
        fake_quality.objects.update_or_create.assert_called_once()


def test_persist_window_db_error_returns_false_and_warns_once(caplog):
    """MySQL 挂了不崩、只告警一次，但要返回 False 让调用方不 ACK。"""
    import stream.consumer as consumer
    consumer._django_ready = True
    consumer._persist_warned = False

    fake_model, fake_quality = MagicMock(), MagicMock()
    fake_model.objects.bulk_create.side_effect = ConnectionError("MySQL gone")
    p1, p2 = _patched_models(fake_model, fake_quality)
    with p1, p2:
        caplog.set_level(logging.WARNING, logger="consumer")

        assert consumer.persist_window(WINDOW_START, _sample_result()) is False
        w1 = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(w1) == 1
        assert "落库失败" in w1[0].getMessage()

        caplog.clear()
        assert consumer.persist_window(
            WINDOW_START + timedelta(seconds=1), _sample_result()
        ) is False
        w2 = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(w2) == 0


def test_persist_window_empty_counts_skips_write():
    """窗口什么事都没发生（total=0 且无迟到/非法）时，两张表都不该写。"""
    import stream.consumer as consumer
    consumer._django_ready = True
    consumer._persist_warned = False

    fake_model, fake_quality = MagicMock(), MagicMock()
    p1, p2 = _patched_models(fake_model, fake_quality)
    with p1, p2:
        empty = {
            "total": 0, "stations": [], "late_count": 0,
            "invalid_count": 0, "late_by_station": {},
        }
        assert consumer.persist_window(WINDOW_START, empty) is True
        fake_model.objects.bulk_create.assert_not_called()
        fake_quality.objects.update_or_create.assert_not_called()


def test_persist_window_late_only_still_persisted():
    """只有迟到事件、正常客流为 0 也要落库，late_count 不能丢。"""
    import stream.consumer as consumer
    consumer._django_ready = True
    consumer._persist_warned = False

    fake_model, fake_quality = MagicMock(), MagicMock()
    p1, p2 = _patched_models(fake_model, fake_quality)
    with p1, p2:
        result = {
            "total": 0, "stations": [], "late_count": 2,
            "invalid_count": 0, "late_by_station": {"A01": 2},
        }
        assert consumer.persist_window(WINDOW_START, result) is True
        fake_model.objects.bulk_create.assert_called_once()

        # MetricWindow 被构造了几次、参数是什么（MagicMock 属性不能直接取值，
        # 必须看构造 call 的 kwargs）
        assert fake_model.call_count == 1
        _, kwargs = fake_model.call_args
        assert kwargs["station_id"] == "A01"
        assert kwargs["passengers"] == 0
        assert kwargs["late_count"] == 2
        assert kwargs["ts"] == WINDOW_START

        # 质量表是窗口级，即使没有正常客流也要写
        _, qkwargs = fake_quality.objects.update_or_create.call_args
        assert qkwargs["ts"] == WINDOW_START
        assert qkwargs["defaults"] == {"late_count": 2, "invalid_count": 0}


def test_persist_window_writes_in_out_split():
    """v1.3：落库必须传 in_passengers/out_passengers，与事件方向一致。"""
    import stream.consumer as consumer
    consumer._django_ready = True
    consumer._persist_warned = False

    result = {
        "total": 7,
        "stations": [
            {"station_id": "A01", "line_id": "L1", "passengers": 7,
             "in_passengers": 5, "out_passengers": 2},
        ],
        "late_count": 0,
        "invalid_count": 0,
        "late_by_station": {},
    }
    fake_model, fake_quality = MagicMock(), MagicMock()
    p1, p2 = _patched_models(fake_model, fake_quality)
    with p1, p2:
        assert consumer.persist_window(WINDOW_START, result) is True
        fake_model.objects.bulk_create.assert_called_once()
        _, kwargs = fake_model.call_args
        assert kwargs["station_id"] == "A01"
        assert kwargs["passengers"] == 7
        assert kwargs["in_passengers"] == 5
        assert kwargs["out_passengers"] == 2


@pytest.mark.django_db
def test_persist_window_updates_row_and_quality_in_real_db():
    """真数据库回归：同键第二次写更大的值必须覆盖，质量表要落一行。"""
    import stream.consumer as consumer
    from api.models import MetricQuality, MetricWindow

    consumer._django_ready = True
    consumer._persist_warned = False

    def result(total):
        return {
            "total": total,
            "stations": [{
                "station_id": "A01", "line_id": "L1", "passengers": total,
                "in_passengers": total, "out_passengers": 0,
            }],
            "late_count": 0,
            "invalid_count": 0,
            "late_by_station": {},
        }

    assert consumer.persist_window(WINDOW_START, result(5)) is True
    assert consumer.persist_window(WINDOW_START, result(9)) is True

    row = MetricWindow.objects.get(ts=WINDOW_START, station_id="A01")
    assert row.passengers == 9
    quality = MetricQuality.objects.get(ts=WINDOW_START)
    assert (quality.late_count, quality.invalid_count) == (0, 0)


# ---- replay 模式测试（采集 backfill 阻塞点修复）----

def test_replay_bucket_groups_by_event_time():
    """回填事件按各自的 event_time 落到历史窗口，而不是全堆在 now。"""
    events = [
        {"station_id": "A01", "line_id": "L1", "passengers": 1,
         "event_time": "2026-10-20T09:30:00.100+08:00"},
        {"station_id": "A01", "line_id": "L1", "passengers": 2,
         "event_time": "2026-10-20T09:30:00.900+08:00"},  # 同一窗口
        {"station_id": "B01", "line_id": "L2", "passengers": 3,
         "event_time": "2026-10-20T09:31:05.000+08:00"},  # 下一窗口
    ]
    buckets = _replay_bucket(events)
    assert len(buckets) == 2
    keys = sorted(buckets)
    assert keys[0] == datetime(2026, 10, 20, 9, 30, 0, tzinfo=TZ)
    assert keys[1] == datetime(2026, 10, 20, 9, 31, 5, tzinfo=TZ)
    assert len(buckets[keys[0]]) == 2
    assert len(buckets[keys[1]]) == 1


def test_replay_bucket_skips_broken_event_time():
    """坏 event_time 的事件不进桶（由 aggregate 记非法），不会让分桶崩掉。"""
    events = [
        {"station_id": "A01", "passengers": 1, "event_time": "not-a-time"},
        {"station_id": "A01", "passengers": 2,
         "event_time": "2026-10-20T09:30:00.100+08:00"},
    ]
    buckets = _replay_bucket(events)
    assert len(buckets) == 1


def test_run_window_replay_does_not_write_snapshot():
    """replay 模式不写 SNAPSHOT_KEY（大屏不能跳），且按历史窗口逐窗落库。"""
    import stream.consumer as consumer
    consumer._django_ready = True
    consumer._persist_warned = False

    payload_a = {"event_id": "e-a", "station_id": "A01", "line_id": "L1",
                 "event_type": "in", "passengers": 2,
                 "event_time": "2026-10-20T09:30:00.100+08:00"}
    payload_b = {"event_id": "e-b", "station_id": "A01", "line_id": "L1",
                 "event_type": "in", "passengers": 3,
                 "event_time": "2026-10-20T09:31:00.100+08:00"}

    class FakeClient:
        def __init__(self):
            self.set_calls = []

        def xreadgroup(self, group, consumer, streams, count=None, block=None):
            return [("gate_events", [
                ("1-0", {"data": json.dumps(payload_a)}),
                ("2-0", {"data": json.dumps(payload_b)}),
            ])]

        def xack(self, stream, group, *ids):
            pass

        def set(self, key, value, ex=None):
            self.set_calls.append(key)

    client = FakeClient()
    state = {"name": "worker-test", "deferred": []}

    with patch.object(consumer, "persist_window") as fake_persist:
        consumer.run_window(client, state, replay=True, first=True)

    # 不写快照
    assert client.set_calls == []
    # 两个历史窗口各落一次库
    assert fake_persist.call_count == 2
    # 落库的 ts 是历史窗口起点，不是 now
    first_ts = fake_persist.call_args_list[0][0][0]
    second_ts = fake_persist.call_args_list[1][0][0]
    assert first_ts == datetime(2026, 10, 20, 9, 30, 0, tzinfo=TZ)
    assert second_ts == datetime(2026, 10, 20, 9, 31, 0, tzinfo=TZ)


# ---- 确认时机回归：deferred 与落库失败都不能提前 ACK ----

class _RunClient:
    def __init__(self, batch=None):
        self.batch = batch
        self.sets = []
        self.acked = []

    def xreadgroup(self, group, consumer, streams, count=None, block=None):
        batch, self.batch = self.batch, None
        return batch

    def set(self, key, value, ex=None):
        self.sets.append(json.loads(value))

    def xack(self, stream, group, *ids):
        self.acked.extend(ids)


class _FixedDateTime:
    current = datetime(2026, 10, 20, 9, 30, 0, 500000, tzinfo=TZ)

    @classmethod
    def now(cls, tz=None):
        return cls.current


def _event(offset_ms, event_id):
    return {
        "event_id": event_id,
        "station_id": "A01",
        "line_id": "L1",
        "event_type": "in",
        "passengers": 2,
        "event_time": (
            datetime(2026, 10, 20, 9, 30, 0, tzinfo=TZ)
            + timedelta(milliseconds=offset_ms)
        ).isoformat(timespec="milliseconds"),
    }


def test_run_window_does_not_ack_deferred_messages(monkeypatch):
    """落在下一窗口的事件先留在 deferred，消息不能当轮 ACK。"""
    import stream.consumer as consumer

    _FixedDateTime.current = datetime(2026, 10, 20, 9, 30, 0, 500000, tzinfo=TZ)
    monkeypatch.setattr(consumer, "datetime", _FixedDateTime)
    monkeypatch.setattr(consumer, "persist_window", lambda *a: True)

    client = _RunClient([("gate_events", [
        ("1-0", {"data": json.dumps(_event(100, "e-ready"))}),
        ("2-0", {"data": json.dumps(_event(1200, "e-future"))}),
    ])])
    state = {"name": "w", "deferred": [], "deferred_ids": [], "persist_retry": []}
    consumer.run_window(client, state, first=True)

    assert client.acked == ["1-0"]
    assert state["deferred_ids"] == ["2-0"]

    # 下一窗口再确认：deferred 的事件被算进去后才 ACK
    _FixedDateTime.current = datetime(2026, 10, 20, 9, 30, 1, 500000, tzinfo=TZ)
    client2 = _RunClient()
    consumer.run_window(client2, state, first=True)
    assert client2.acked == ["2-0"]
    assert state["deferred"] == [] and state["deferred_ids"] == []


def test_run_window_retries_failed_persist_before_ack(monkeypatch):
    """落库失败不 ACK；下一轮补写成功后再确认。"""
    import stream.consumer as consumer

    _FixedDateTime.current = datetime(2026, 10, 20, 9, 30, 0, 500000, tzinfo=TZ)
    monkeypatch.setattr(consumer, "datetime", _FixedDateTime)

    calls = []

    def fake_persist(ts, result):
        calls.append(ts)
        return len(calls) > 1  # 第一次失败，之后成功

    monkeypatch.setattr(consumer, "persist_window", fake_persist)

    client = _RunClient([("gate_events", [
        ("1-0", {"data": json.dumps(_event(100, "e-retry"))}),
    ])])
    state = {"name": "w", "deferred": [], "deferred_ids": [], "persist_retry": []}
    consumer.run_window(client, state, first=True)

    assert client.acked == []
    assert len(state["persist_retry"]) == 1

    client2 = _RunClient()
    consumer.run_window(client2, state, first=True)
    assert client2.acked == ["1-0"]
    assert state["persist_retry"] == []


def test_run_window_replay_does_not_ack_on_persist_failure(monkeypatch):
    """回放时任一窗口落库失败，整批消息保持未确认，重跑可补。"""
    import stream.consumer as consumer

    monkeypatch.setattr(consumer, "persist_window", lambda *a: False)
    client = _RunClient([("gate_events", [
        ("1-0", {"data": json.dumps(_event(100, "e-r1"))}),
    ])])
    state = {"name": "w", "deferred": [], "deferred_ids": [], "persist_retry": []}
    consumer.run_window(client, state, replay=True, first=True)
    assert client.acked == []
