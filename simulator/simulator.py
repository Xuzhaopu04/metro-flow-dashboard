"""闸机事件模拟器。

按站点和时段生成进/出站刷卡记录，写进 Redis Stream 供计算端消费。

用法：
    python simulator/simulator.py                    # 每秒 1 条，一直发
    python simulator/simulator.py --speed 5 --count 200
    python simulator/simulator.py --inject 0.05      # 5% 异常数据，测消费端容错
    python simulator/simulator.py --scenario evening_peak --speed 10
    python simulator/simulator.py --backfill 60      # 回填过去 60 分钟
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

# 站点表外置到 stations.json，改站点不碰代码；A 线是主干线，权重比 B 线高
# 结构直接对应契约 4.5：顶层是 station_id，服务端每次原样读取，不要再包一层
_STATIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stations.json")
try:
    with open(_STATIONS_FILE, encoding="utf-8") as _fp:
        STATIONS = json.load(_fp)
except FileNotFoundError:
    print(f"找不到站点配置文件 {_STATIONS_FILE}，请确认文件存在")
    sys.exit(1)
except json.JSONDecodeError as exc:
    print(f"站点配置文件 {_STATIONS_FILE} 格式错误：{exc}")
    sys.exit(1)

# 早高峰进站多，晚高峰出站多，平时各半
PEAK_HOURS_IN = range(7, 10)
PEAK_HOURS_OUT = range(17, 20)

# 固定场景用于演示和压测；auto 继续按当前时钟判断
SCENARIOS = {
    "auto": None,
    "morning_peak": {"in_ratio": 0.78, "max_passengers": 5},
    "evening_peak": {"in_ratio": 0.28, "max_passengers": 5},
    "offpeak": {"in_ratio": 0.5, "max_passengers": 3},
    "burst": {"in_ratio": 0.5, "max_passengers": 8},
}


def pick_station():
    ids = list(STATIONS)
    weights = [STATIONS[s]["weight"] for s in ids]
    return random.choices(ids, weights=weights)[0]


def passenger_upper(now, scenario):
    rule = SCENARIOS.get(scenario)
    if rule is not None:
        return rule["max_passengers"]
    return 5 if now.hour in PEAK_HOURS_IN or now.hour in PEAK_HOURS_OUT else 3


def pick_direction(now, scenario="auto"):
    rule = SCENARIOS.get(scenario)
    if rule is not None:
        return "in" if random.random() < rule["in_ratio"] else "out"
    hour = now.hour
    if hour in PEAK_HOURS_IN:
        return "in" if random.random() < 0.78 else "out"
    if hour in PEAK_HOURS_OUT:
        return "out" if random.random() < 0.72 else "in"
    return "in" if random.random() < 0.5 else "out"


def make_event(now=None, scenario="auto"):
    now = now or datetime.now(TZ)
    station_id = pick_station()
    line_id = STATIONS[station_id]["line_id"]
    direction = pick_direction(now, scenario)
    return {
        "event_id": str(uuid.uuid4()),
        "station_id": station_id,
        "line_id": line_id,
        "event_type": direction,
        "passengers": random.randint(1, passenger_upper(now, scenario)),
        "event_time": now.isoformat(timespec="milliseconds"),
    }


def corrupt(event, now):
    """按契约 2.2 造两类异常：缺字段、乱序。"""
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
    """按原时间间隔重放样例文件，用于演示和回归。

    样例里的绝对时间戳不能直接用（回放出来全是历史时间），
    所以把第一条对齐到当前时间，后续按原相对间隔顺延。
    """
    events = []
    try:
        with open(args.replay, encoding="utf-8") as fp:
            for lineno, line in enumerate(fp, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    print(f"样例文件第 {lineno} 行 JSON 解析失败：{exc}")
                    sys.exit(1)
    except FileNotFoundError:
        print(f"找不到样例文件 {args.replay}")
        sys.exit(1)
    if not events:
        print("样例文件是空的")
        return 0

    speed = max(args.speed, 0.01)
    print(f"回放 {len(events)} 条，倍速 {speed}")

    # 整体平移：offset = 当前时间 - 样例首条时间，每条都加这个 offset
    now = datetime.now(TZ)
    first_stamp = datetime.fromisoformat(events[0]["event_time"])
    offset = now - first_stamp

    previous = None
    sent = 0
    for event in events:
        original = datetime.fromisoformat(event["event_time"])
        shifted = original + offset
        event["event_time"] = shifted.isoformat(timespec="milliseconds")
        if previous is not None:
            time.sleep(max(0.0, (shifted - previous).total_seconds() / speed))
        previous = shifted
        emit(client, event, args.dry_run)
        sent += 1
        print(f"[{sent}] {event['station_id']} {event['event_type']} {event.get('passengers')}人")
    return sent


def stream_forever(args, client):
    interval = 1.0 / max(args.speed, 0.01)
    sent = 0
    while args.count == 0 or sent < args.count:
        now = datetime.now(TZ)
        event = make_event(now, args.scenario)
        if args.inject > 0 and random.random() < args.inject:
            event = corrupt(event, now)
        emit(client, event, args.dry_run)
        sent += 1
        print(f"[{sent}] {event['station_id']} {event['event_type']} {event.get('passengers')}人")
        time.sleep(interval)
    return sent


def backfill(args, client):
    """按秒回填过去 N 分钟的事件，给查询页准备历史数据。

    注意：消费端只接受窗口起点前 2 个窗口内的事件，更早的会被记成迟到并跳过。
    要让回填真正落库，需要计算侧临时放宽迟到容忍度，否则这里只验证链路。
    """
    minutes = args.backfill
    total = minutes * 60
    end = datetime.now(TZ)
    start = end - timedelta(minutes=minutes)
    print(f"回填 {minutes} 分钟，共 {total} 条")
    print(f"时间范围 {start.isoformat(timespec='seconds')} — {end.isoformat(timespec='seconds')}")
    print("提示：早于消费端迟到窗口的事件会被判迟到，历史落库需计算侧配合")

    sent = 0
    for second in range(total):
        moment = start + timedelta(seconds=second)
        event = make_event(moment, args.scenario)
        if args.inject > 0 and random.random() < args.inject:
            event = corrupt(event, moment)
        emit(client, event, args.dry_run)
        sent += 1
        if sent % 600 == 0:
            print(f"已回填 {sent}/{total}")
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
    parser.add_argument("--scenario", default="auto", choices=list(SCENARIOS),
                        help="场景：auto 按当前时段，其余为固定演示场景")
    parser.add_argument("--backfill", type=int, default=0, help="回填过去 N 分钟，0 表示不回填")
    parser.add_argument("--replay", help="按样例文件重放，传入 JSONL 路径")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写队列")
    args = parser.parse_args()

    if not 0 <= args.inject <= 1:
        parser.error("--inject 取值应在 0 到 1 之间")

    if args.count < 0:
        parser.error("--count 不能为负数")

    if args.backfill < 0:
        parser.error("--backfill 不能为负数")

    if args.replay and args.backfill:
        parser.error("--replay 与 --backfill 只能选一个")

    client = build_client(args.dry_run)
    sent = 0
    try:
        if args.replay:
            sent = replay(args, client)
        elif args.backfill:
            sent = backfill(args, client)
        else:
            sent = stream_forever(args, client)
    except KeyboardInterrupt:
        pass
    finally:
        if client is not None:
            client.close()
    print(f"\n共发送 {sent} 条")


if __name__ == "__main__":
    main()
