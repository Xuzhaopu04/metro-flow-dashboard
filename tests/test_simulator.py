"""采集模块（闸机模拟器）测试。

覆盖四个核心场景：事件生成、速率控制、异常注入、回放。
用假客户端截获 emit，不依赖 Redis。
"""

import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from simulator import simulator as sim

TZ = timezone(timedelta(hours=8))


class FakeRedis:
    """只接住 xadd，不连真实 Redis。"""

    def __init__(self):
        self.sent = []

    def xadd(self, stream, fields, maxlen=None, approximate=None):
        self.sent.append(json.loads(fields["data"]))


# ---------- 1. 事件生成 ----------

def test_make_event_has_all_contract_fields():
    """契约 2.1 的六个字段都要有，类型要对。"""
    event = sim.make_event(now=datetime(2026, 11, 10, 9, 30, 0, 123000, tzinfo=TZ))
    assert set(event.keys()) == {"event_id", "station_id", "line_id", "event_type", "passengers", "event_time"}
    assert isinstance(event["event_id"], str) and len(event["event_id"]) > 0
    assert event["station_id"] in sim.STATIONS
    assert event["line_id"] == sim.STATIONS[event["station_id"]]["line_id"]
    assert event["event_type"] in ("in", "out")
    assert isinstance(event["passengers"], int) and 1 <= event["passengers"] <= 5
    # 时间是 ISO8601 带毫秒带时区
    parsed = datetime.fromisoformat(event["event_time"])
    assert parsed.tzinfo is not None


def test_make_event_peak_hours_allow_more_passengers():
    """高峰期 passengers 上限 5，平峰上限 3。"""
    peak = sim.make_event(now=datetime(2026, 11, 10, 8, 30, 0, tzinfo=TZ))  # 早高峰
    offpeak = sim.make_event(now=datetime(2026, 11, 10, 14, 0, 0, tzinfo=TZ))  # 平峰
    assert 1 <= peak["passengers"] <= 5
    assert 1 <= offpeak["passengers"] <= 3


def test_make_event_morning_peak_biases_inbound():
    """早高峰 78% 概率进站，跑 200 次进站应明显多于出站。"""
    ins = sum(
        1 for _ in range(200)
        if sim.make_event(now=datetime(2026, 11, 10, 8, 0, 0, tzinfo=TZ))["event_type"] == "in"
    )
    assert ins > 130  # 期望约 156，留余量


# ---------- 2. 速率控制 ----------

def test_stream_forever_respects_count():
    """--count N 恰好发 N 条。"""
    client = FakeRedis()
    args = SimpleNamespace(speed=100, count=5, inject=0.0, dry_run=False)
    with mock.patch.object(time, "sleep"):
        sim.stream_forever(args, client)
    assert len(client.sent) == 5


def test_stream_forever_uses_inverse_speed_as_interval():
    """发送间隔 = 1 / speed。"""
    client = FakeRedis()
    args = SimpleNamespace(speed=4, count=2, inject=0.0, dry_run=False)
    sleeps = []
    with mock.patch.object(time, "sleep", side_effect=lambda s: sleeps.append(s)):
        sim.stream_forever(args, client)
    # 第二条之后才睡，间隔约 0.25 秒
    assert abs(sleeps[0] - 0.25) < 1e-6


# ---------- 3. 异常注入 ----------

def test_corrupt_missing_field_removes_passengers_or_event_time():
    """缺字段类异常只删 passengers 或 event_time。"""
    now = datetime(2026, 11, 10, 9, 30, 0, tzinfo=TZ)
    with mock.patch.object(sim.random, "choice", return_value="missing_field"):
        with mock.patch.object(sim.random, "choice", side_effect=["missing_field", "passengers"]):
            event = sim.make_event(now=now)
            corrupted = sim.corrupt(dict(event), now)
    assert "passengers" not in corrupted
    # station_id 等其余字段仍在
    assert corrupted["station_id"] == event["station_id"]


def test_corrupt_out_of_order_sets_earlier_event_time():
    """乱序类异常把 event_time 改成过去。"""
    now = datetime(2026, 11, 10, 9, 30, 0, tzinfo=TZ)
    event = sim.make_event(now=now)
    original_time = datetime.fromisoformat(event["event_time"])
    with mock.patch.object(sim.random, "choice", return_value="out_of_order"):
        with mock.patch.object(sim.random, "randint", return_value=10):
            corrupted = sim.corrupt(dict(event), now)
    shifted = datetime.fromisoformat(corrupted["event_time"])
    assert shifted < original_time


# ---------- 4. 回放 ----------

def _write_sample(tmp_path, events):
    path = tmp_path / "sample.jsonl"
    with open(path, "w", encoding="utf-8") as fp:
        for e in events:
            fp.write(json.dumps(e, ensure_ascii=False) + "\n")
    return str(path)


def test_replay_shifts_timestamps_to_now(tmp_path):
    """回放时样例时间戳整体平移到当前时间附近。"""
    sample = [
        {"event_id": "1", "station_id": "A01", "line_id": "L1", "event_type": "in",
         "passengers": 2, "event_time": "2026-01-01T00:00:00.000+08:00"},
        {"event_id": "2", "station_id": "A02", "line_id": "L1", "event_type": "out",
         "passengers": 1, "event_time": "2026-01-01T00:00:00.500+08:00"},
    ]
    path = _write_sample(tmp_path, sample)
    fake_now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=TZ)
    client = FakeRedis()
    args = SimpleNamespace(replay=path, speed=1000, dry_run=False)

    with mock.patch.object(sim, "datetime") as fake_dt, mock.patch.object(time, "sleep"):
        fake_dt.now.return_value = fake_now
        fake_dt.fromisoformat = datetime.fromisoformat
        fake_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        sim.replay(args, client)

    assert len(client.sent) == 2
    first = datetime.fromisoformat(client.sent[0]["event_time"])
    second = datetime.fromisoformat(client.sent[1]["event_time"])
    # 首条对齐到 fake_now
    assert first == fake_now
    # 相对间隔 0.5 秒保留
    assert (second - first).total_seconds() == 0.5


def test_replay_empty_file_returns_zero(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    args = SimpleNamespace(replay=str(path), speed=1, dry_run=True)
    assert sim.replay(args, None) == 0
