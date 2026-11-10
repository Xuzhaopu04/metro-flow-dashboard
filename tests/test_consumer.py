"""消费端的取数与脏数据处理测试，用假客户端代替 Redis。"""

import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from stream.consumer import ensure_group, read_batch

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
    assert events == [{}]
    # 坏消息也要拿到 ID，否则会一直挂在待确认列表里
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


# 注意：persist_window 里是延迟 import（`from api.models import MetricWindow`），
# 所以 patch 目标必须是 api.models.MetricWindow，而不是 stream.consumer.MetricWindow。
_PATCH_TARGET = "api.models.MetricWindow"


def test_persist_window_bulk_create_has_ignore_conflicts():
    """落库必须传 ignore_conflicts=True — 幂等写入的关键（指标口径 §4 "重放不产生重复行"）。"""
    import stream.consumer as consumer
    consumer._django_ready = True
    consumer._persist_warned = False

    fake_model = MagicMock()
    with patch(_PATCH_TARGET, fake_model):
        consumer.persist_window(WINDOW_START, _sample_result())
        fake_model.objects.bulk_create.assert_called_once()
        _, kwargs = fake_model.objects.bulk_create.call_args
        assert kwargs.get("ignore_conflicts") is True


def test_persist_window_db_error_only_warns_once(caplog):
    """MySQL 挂了不崩，且只打一条 warning（consumer.py 注释："只告警一次"）。"""
    import stream.consumer as consumer
    consumer._django_ready = True
    consumer._persist_warned = False

    fake_model = MagicMock()
    fake_model.objects.bulk_create.side_effect = ConnectionError("MySQL gone")
    with patch(_PATCH_TARGET, fake_model):
        caplog.set_level(logging.WARNING, logger="consumer")

        # 第一次调用：应打一条 warning
        consumer.persist_window(WINDOW_START, _sample_result())
        w1 = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(w1) == 1
        assert "落库失败" in w1[0].getMessage()

        # 第二次调用：不再打 warning
        caplog.clear()
        consumer.persist_window(WINDOW_START + timedelta(seconds=1), _sample_result())
        w2 = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(w2) == 0


def test_persist_window_empty_counts_skips_write():
    """窗口什么事都没发生（total=0 且无迟到）时，不应调 bulk_create。"""
    import stream.consumer as consumer
    consumer._django_ready = True
    consumer._persist_warned = False

    fake_model = MagicMock()
    with patch(_PATCH_TARGET, fake_model):
        empty = {
            "total": 0, "stations": [], "late_count": 0,
            "invalid_count": 0, "late_by_station": {},
        }
        consumer.persist_window(WINDOW_START, empty)
        fake_model.objects.bulk_create.assert_not_called()


def test_persist_window_late_only_still_persisted():
    """只有迟到事件、正常客流为 0 也要落库，late_count 不能丢。"""
    import stream.consumer as consumer
    consumer._django_ready = True
    consumer._persist_warned = False

    fake_model = MagicMock()
    with patch(_PATCH_TARGET, fake_model):
        result = {
            "total": 0, "stations": [], "late_count": 2,
            "invalid_count": 0, "late_by_station": {"A01": 2},
        }
        consumer.persist_window(WINDOW_START, result)
        fake_model.objects.bulk_create.assert_called_once()

        # MetricWindow 被构造了几次、参数是什么（MagicMock 属性不能直接取值，
        # 必须看构造 call 的 kwargs）
        assert fake_model.call_count == 1
        _, kwargs = fake_model.call_args
        assert kwargs["station_id"] == "A01"
        assert kwargs["passengers"] == 0
        assert kwargs["late_count"] == 2
        assert kwargs["ts"] == WINDOW_START
