"""实时消费与窗口聚合。

从 Redis Stream 用消费组读取闸机事件，按窗口聚合出站点客流，
一路写 Redis 快照给接口读，一路按站点落 MySQL 供历史查询。

用法：
    python stream/consumer.py            # 常驻
    python stream/consumer.py --once     # 只跑一个窗口就退出，便于自测
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

import redis

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from stream.metrics import aggregate_window, split_ready, window_start_of  # noqa: E402

TZ = timezone(timedelta(hours=8))

REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
EVENT_STREAM = os.environ.get("EVENT_STREAM", "gate_events")
CONSUMER_GROUP = os.environ.get("CONSUMER_GROUP", "metric_builder")
SNAPSHOT_KEY = os.environ.get("SNAPSHOT_KEY", "metric:snapshot")

WINDOW_SECONDS = int(os.environ.get("WINDOW_SECONDS", "1"))
SNAPSHOT_TTL = int(os.environ.get("SNAPSHOT_TTL", "5"))
LATE_TOLERANCE_WINDOWS = int(os.environ.get("LATE_TOLERANCE_WINDOWS", "2"))

BATCH_SIZE = 500
BLOCK_MS = 500
MAX_DEFERRED = 2000

log = logging.getLogger("consumer")

_running = True
_django_ready = False
_persist_warned = False


def ensure_group(client):
    """建消费组。已存在时 Redis 会抛 BUSYGROUP，那不是错误。"""
    try:
        # 从 0 开始，第一次启动也能把已经积压的事件算进去
        client.xgroup_create(EVENT_STREAM, CONSUMER_GROUP, id="0", mkstream=True)
        log.info("已创建消费组 %s", CONSUMER_GROUP)
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def read_batch(client, consumer_name):
    """读一批新事件，返回 (事件列表, 消息 ID 列表)。"""
    response = client.xreadgroup(
        CONSUMER_GROUP,
        consumer_name,
        {EVENT_STREAM: ">"},
        count=BATCH_SIZE,
        block=BLOCK_MS,
    )
    events, msg_ids = [], []
    for _, messages in response or []:
        for msg_id, fields in messages:
            msg_ids.append(msg_id)
            raw = fields.get("data")
            if not raw:
                continue
            try:
                events.append(json.loads(raw))
            except json.JSONDecodeError:
                # 脏消息当成空事件，会进 invalid_count，不会让循环挂掉
                events.append({})
    return events, msg_ids


def build_snapshot(window_start, result):
    return {
        "ts": window_start.isoformat(timespec="seconds"),
        "total": result["total"],
        "stations": result["stations"],
        "late_count": result["late_count"],
        "invalid_count": result["invalid_count"],
    }


def persist_window(window_start, result):
    """按站点写入 metric_window。写库失败只降级，不影响快照。"""
    global _django_ready, _persist_warned

    counts = {s["station_id"]: s["passengers"] for s in result["stations"]}
    for station_id in result["late_by_station"]:
        counts.setdefault(station_id, 0)
    if not counts:
        return

    try:
        if not _django_ready:
            import django

            os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
            django.setup()
            _django_ready = True

        from api.models import MetricWindow

        rows = [
            MetricWindow(
                ts=window_start,
                station_id=station_id,
                passengers=passengers,
                late_count=result["late_by_station"].get(station_id, 0),
            )
            for station_id, passengers in counts.items()
        ]
        MetricWindow.objects.bulk_create(rows, ignore_conflicts=True)
        _persist_warned = False
    except Exception as exc:  # 进程边界：数据库挂了也不能让采集停摆
        if not _persist_warned:
            log.warning("落库失败（%s），先只写快照：%s", type(exc).__name__, exc)
            _persist_warned = True


def run_window(client, state, first=False):
    """跑一个窗口：读数、切窗、聚合、写快照、落库、确认消息。"""
    started = time.monotonic()
    window_start = window_start_of(datetime.now(TZ), WINDOW_SECONDS)
    window_end = window_start + timedelta(seconds=WINDOW_SECONDS)

    events, msg_ids = read_batch(client, state["name"])
    ready, state["deferred"] = split_ready(state["deferred"] + events, window_end)
    if len(state["deferred"]) > MAX_DEFERRED:
        state["deferred"] = state["deferred"][-MAX_DEFERRED:]

    result = aggregate_window(ready, window_start, WINDOW_SECONDS, LATE_TOLERANCE_WINDOWS)
    snapshot = build_snapshot(window_start, result)
    client.set(SNAPSHOT_KEY, json.dumps(snapshot, ensure_ascii=False), ex=SNAPSHOT_TTL)
    persist_window(window_start, result)

    if msg_ids:
        client.xack(EVENT_STREAM, CONSUMER_GROUP, *msg_ids)

    log.info(
        "%s 客流 %d 站点 %d 迟到 %d 非法 %d",
        snapshot["ts"],
        snapshot["total"],
        len(snapshot["stations"]),
        snapshot["late_count"],
        snapshot["invalid_count"],
    )

    if not first:
        # 扣掉本轮耗时，否则每轮多睡 1 秒，窗口越跑越偏
        time.sleep(max(0.0, WINDOW_SECONDS - (time.monotonic() - started)))


def main() -> int:
    parser = argparse.ArgumentParser(description="实时指标消费者")
    parser.add_argument("--once", action="store_true", help="只跑一个窗口，用于自测")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=True,
        socket_connect_timeout=2,
    )
    try:
        client.ping()
    except redis.RedisError:
        log.error("连不上 Redis %s:%s，先执行 docker compose up -d", REDIS_HOST, REDIS_PORT)
        return 1

    ensure_group(client)
    state = {"name": f"worker-{os.getpid()}", "deferred": []}
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    log.info("消费者 %s 启动，订阅 %s，窗口 %ss", state["name"], EVENT_STREAM, WINDOW_SECONDS)

    try:
        if args.once:
            run_window(client, state, first=True)
            return 0
        while _running:
            run_window(client, state)
    except redis.RedisError as exc:
        log.error("Redis 连接中断：%s", exc)
        return 1
    finally:
        client.close()
        log.info("已退出")

    return 0


def _stop(signum, frame):
    global _running
    _running = False
    log.info("收到信号 %s，处理完当前窗口后退出", signum)


if __name__ == "__main__":
    sys.exit(main())
