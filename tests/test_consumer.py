"""消费端的取数与脏数据处理测试，用假客户端代替 Redis。"""

import json

from stream.consumer import ensure_group, read_batch


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
