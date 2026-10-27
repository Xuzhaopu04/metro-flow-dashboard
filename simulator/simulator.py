"""闸机事件模拟器。

按站点和时段生成进/出站刷卡记录，写进 Redis Stream 供计算端消费。

用法：
    python simulator/simulator.py                    # 每秒 1 条，一直发
    python simulator/simulator.py --speed 5 --count 200
    python simulator/simulator.py --inject 0.05      # 5% 异常数据，测消费端容错
    python simulator/simulator.py --replay data/samples/sample_events.json
    python simulator/simulator.py --dry-run          # 只打印，不写 Redis
"""

import argparse
import json
import os
import random
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.env import load_env  # noqa: E402

# 下面这些常量在模块导入时就读完，必须在此之前加载 .env
load_env()

TZ = timezone(timedelta(hours=8))

REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
EVENT_STREAM = os.environ.get("EVENT_STREAM", "gate_events")
STREAM_MAXLEN = 100000

# 站点:(线路, 客流权重)。A 线是主干线，人比 B 线多
STATIONS = {
    "A01": ("L1", 0.24),
    "A02": ("L1", 0.18),
    "A03": ("L1", 0.12),
    "B01": ("L2", 0.16),
    "B02": ("L2", 0.10),
    "B03": ("L2", 0.20),
}

# 早高峰进站多，晚高峰出站多，平时各半
PEAK_HOURS_IN = range(7, 10)
PEAK_HOURS_OUT = range(17, 20)


def pick_station():
    ids = list(STATIONS)
    weights = [STATIONS[s][1] for s in ids]
    return random.choices(ids, weights=weights)[0]


def pick_direction(now):
    hour = now.hour
    if hour in PEAK_HOURS_IN:
        return "in" if random.random() < 0.78 else "out"
    if hour in PEAK_HOURS_OUT:
        return "out" if random.random() < 0.72 else "in"
    return "in" if random.random() < 0.5 else "out"


def make_event(now=None):
    now = now or datetime.now(TZ)
    station_id = pick_station()
    line_id = STATIONS[station_id][0]
    direction = pick_direction(now)
    # 高峰期单笔刷卡带的人也多一些
    upper = 5 if now.hour in PEAK_HOURS_IN or now.hour in PEAK_HOURS_OUT else 3
    return {
        "event_id": str(uuid.uuid4()),
        "station_id": station_id,
        "line_id": line_id,
        "event_type": direction,
        "passengers": random.randint(1, upper),
        "event_time": now.isoformat(timespec="milliseconds"),
    }


def corrupt(event, now):
    """按契约 2.3 造两类异常：缺字段、乱序。"""
    kind = random.choice(["missing_field", "out_of_order"])
    if kind == "missing_field":
        event.pop(random.choice(["passengers", "event_time"]), None)
    else:
        backdated = now - timedelta(seconds=random.randint(3, 30))
        event["event_time"] = backdated.isoformat(timespec="milliseconds")
    return event


def emit(client, event, dry_run):
    if dry_run or client is None:
        return
    client.xadd(EVENT_STREAM, {"data": json.dumps(event, ensure_ascii=False)},
                maxlen=STREAM_MAXLEN, approximate=True)


def replay(args, client):
    """按原时间间隔重放样例文件，用于演示和回归。"""
    with open(args.replay, encoding="utf-8") as fp:
        events = [json.loads(line) for line in fp if line.strip()]
    if not events:
        print("样例文件是空的")
        return 0

    speed = max(args.speed, 0.01)
    print(f"回放 {len(events)} 条，倍速 {speed}")
    previous = None
    sent = 0
    for event in events:
        stamp = datetime.fromisoformat(event["event_time"])
        if previous is not None:
            time.sleep(max(0.0, (stamp - previous).total_seconds() / speed))
        previous = stamp
        emit(client, event, args.dry_run)
        sent += 1
        print(f"[{sent}] {event['station_id']} {event['event_type']} {event.get('passengers')}人")
    return sent


def stream_forever(args, client):
    interval = 1.0 / max(args.speed, 0.01)
    sent = 0
    while args.count == 0 or sent < args.count:
        now = datetime.now(TZ)
        event = make_event(now)
        if args.inject > 0 and random.random() < args.inject:
            event = corrupt(event, now)
        emit(client, event, args.dry_run)
        sent += 1
        print(f"[{sent}] {event['station_id']} {event['event_type']} {event.get('passengers')}人")
        time.sleep(interval)
    return sent


def build_client(dry_run):
    if dry_run:
        return None
    import redis

    client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=True,
        socket_connect_timeout=2,
    )
    try:
        client.ping()
    except redis.RedisError:
        print(f"连不上 Redis {REDIS_HOST}:{REDIS_PORT}，先执行 docker compose up -d")
        raise SystemExit(1)
    return client


def main():
    parser = argparse.ArgumentParser(description="闸机事件模拟器")
    parser.add_argument("--speed", type=float, default=1.0, help="倍速，1 表示每秒 1 条")
    parser.add_argument("--count", type=int, default=0, help="发送条数，0 表示不限")
    parser.add_argument("--inject", type=float, default=0.0, help="异常数据比例，0—1")
    parser.add_argument("--replay", help="按样例文件重放，传入 JSONL 路径")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写队列")
    args = parser.parse_args()

    if not 0 <= args.inject <= 1:
        parser.error("--inject 取值应在 0 到 1 之间")

    client = build_client(args.dry_run)
    sent = 0
    try:
        sent = replay(args, client) if args.replay else stream_forever(args, client)
    except KeyboardInterrupt:
        pass
    finally:
        if client is not None:
            client.close()
    print(f"\n共发送 {sent} 条")


if __name__ == "__main__":
    main()
