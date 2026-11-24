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
from config.env import load_env  # noqa: E402
from stream.metrics import (  # noqa: E402
    aggregate_window,
    parse_time,
    split_ready_indices,
    window_start_of,
)

# 下面这些常量在模块导入时就读完，必须在此之前加载 .env
load_env()

TZ = timezone(timedelta(hours=8))

REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
EVENT_STREAM = os.environ.get("EVENT_STREAM", "gate_events")
CONSUMER_GROUP = os.environ.get("CONSUMER_GROUP", "metric_builder")
CONSUMER_NAME = os.environ.get("CONSUMER_NAME", "metric-builder")
SNAPSHOT_KEY = os.environ.get("SNAPSHOT_KEY", "metric:snapshot")

WINDOW_SECONDS = int(os.environ.get("WINDOW_SECONDS", "1"))
SNAPSHOT_TTL = int(os.environ.get("SNAPSHOT_TTL", "5"))
LATE_TOLERANCE_WINDOWS = int(os.environ.get("LATE_TOLERANCE_WINDOWS", "2"))

BATCH_SIZE = 500
BLOCK_MS = 500
MAX_DEFERRED = 2000
# 落库失败时先留在内存里重试；上限只防内存无限增长，超出部分保持未确认
MAX_PERSIST_RETRY = 60

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


def read_batch(client, consumer_name, read_id=">"):
    """读一批事件，返回 (事件列表, 消息 ID 列表)。

    read_id 用 ">" 读新消息；启动时用 "0" 读本消费者遗留的 pending。
    缺 data 字段的消息不能再跳过：跳过会连同 ACK 一起把一条坏消息抹掉，
    口径上应该记一条非法。这里统一塞空事件，交给 aggregate_window 计数。
    """
    response = client.xreadgroup(
        CONSUMER_GROUP,
        consumer_name,
        {EVENT_STREAM: read_id},
        count=BATCH_SIZE,
        block=BLOCK_MS if read_id == ">" else None,
    )
    events, msg_ids = [], []
    for _, messages in response or []:
        for msg_id, fields in messages:
            msg_ids.append(msg_id)
            raw = fields.get("data")
            if not raw:
                events.append({})
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


def persist_window(window_start, result) -> bool:
    """按站点写入 metric_window，并写窗口级 metric_quality，返回是否成功。

    v1.3 加法：同时写 in_passengers / out_passengers，保证 passengers（乘降量）
    = in_passengers + out_passengers。

    写库失败必须返回 False：调用方据此不 ACK。若在这里吞掉异常还让消息被确认，
    数据库恢复后也补不回这段历史，等于把一次抖动变成永久缺口。

    同键改为 update_conflicts：重试或重放时以最新聚合覆盖。
    ignore_conflicts 只能保证不重复，会把后到的、更完整的结果丢掉。
    """
    global _django_ready, _persist_warned

    # 从 stations 提取按站的 passengers / in / out；迟到站 passengers 为 0 也得落一行
    per_station = {
        s["station_id"]: {
            "passengers": s["passengers"],
            "in_passengers": s.get("in_passengers", 0),
            "out_passengers": s.get("out_passengers", 0),
        }
        for s in result["stations"]
    }
    for station_id in result["late_by_station"]:
        per_station.setdefault(
            station_id, {"passengers": 0, "in_passengers": 0, "out_passengers": 0}
        )

    try:
        if not _django_ready:
            import django

            os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
            django.setup()
            _django_ready = True

        from api.models import MetricQuality, MetricWindow
        from django.db import connection

        rows = [
            MetricWindow(
                ts=window_start,
                station_id=station_id,
                passengers=vals["passengers"],
                in_passengers=vals["in_passengers"],
                out_passengers=vals["out_passengers"],
                late_count=result["late_by_station"].get(station_id, 0),
            )
            for station_id, vals in per_station.items()
        ]
        if rows:
            # MySQL 的 ON DUPLICATE KEY UPDATE 不接受 unique_fields，
            # SQLite 的 ON CONFLICT DO UPDATE 又必须指定冲突列
            upsert = {
                "update_conflicts": True,
                "update_fields": [
                    "passengers",
                    "in_passengers",
                    "out_passengers",
                    "late_count",
                ],
            }
            if connection.vendor != "mysql":
                upsert["unique_fields"] = ["ts", "station_id"]
            MetricWindow.objects.bulk_create(rows, **upsert)
        # 质量计数是窗口级：非法事件可能连站点都没有，不能等 per_station 非空才写
        if rows or result["late_count"] or result["invalid_count"]:
            MetricQuality.objects.update_or_create(
                ts=window_start,
                defaults={
                    "late_count": result["late_count"],
                    "invalid_count": result["invalid_count"],
                },
            )
        _persist_warned = False
        return True
    except Exception as exc:  # 进程边界：数据库挂了也不能让采集停摆
        if not _persist_warned:
            log.warning("落库失败（%s），稍后重试：%s", type(exc).__name__, exc)
            _persist_warned = True
        return False


def _replay_bucket(events):
    """replay 模式下按 event_time 把事件分到各自的历史窗口。

    正常模式的 deferred/split_ready 是为实时流"等窗口关闭"设计的，
    replay 全是历史事件，直接按窗口起点分桶即可，不用等。
    """
    from collections import defaultdict

    buckets = defaultdict(list)
    for event in events:
        try:
            ts = parse_time(event["event_time"])
        except (KeyError, TypeError, ValueError):
            continue  # 脏事件 replay 也不处理，算进 invalid 的在 aggregate 里
        ws = window_start_of(ts, WINDOW_SECONDS)
        buckets[ws].append(event)
    return buckets


def _retry_pending(client, state):
    """补写上一轮落库失败的窗口；写成功后才确认对应消息。

    快照已经发出去了，补写只影响历史与排行。重试队列放在内存里，
    进程重启会丢，但消息仍未 ACK，重启后还能重新投递。
    """
    pending = state.get("persist_retry") or []
    if not pending:
        return

    remaining = []
    for item in pending:
        if persist_window(item["ts"], item["result"]):
            if item["ids"]:
                client.xack(EVENT_STREAM, CONSUMER_GROUP, *item["ids"])
            log.info(
                "补写窗口 %s 成功，确认 %d 条消息",
                item["ts"].isoformat(timespec="seconds"),
                len(item["ids"]),
            )
        else:
            remaining.append(item)

    state["persist_retry"] = remaining
    if len(remaining) > MAX_PERSIST_RETRY:
        dropped = len(remaining) - MAX_PERSIST_RETRY
        state["persist_retry"] = remaining[-MAX_PERSIST_RETRY:]
        log.warning(
            "落库重试队列超过 %d 个窗口，丢弃最早 %d 个（消息保持未确认）",
            MAX_PERSIST_RETRY,
            dropped,
        )


def run_window(client, state, replay=False, first=False, read_id=">"):
    """跑一个窗口：读数、切窗、聚合、写快照、落库、确认消息。

    read_id="0" 用于启动时补消费本消费者遗留的 pending；正常循环用 ">"。

    replay=True 用于 backfill/回填：
      - 事件按 event_time 分桶，各自落在正确的历史窗口
      - 逐窗聚合、逐窗落库（ts 是历史窗口起点，不是 now）
      - 不写 SNAPSHOT_KEY，避免大屏被历史数据冲掉
      - 跳过迟到判定（ignore_late=True）

    消息只在对应窗口落库成功后 ACK。落在下一窗口的事件先留在 deferred，
    它的消息 ID 也一起留到下一轮，不能在当轮提前确认。
    """
    started = time.monotonic()
    events, msg_ids = read_batch(client, state["name"], read_id=read_id)

    # 启动补消费时，没有 pending 就退出，别用空快照把刚恢复的大屏冲掉
    if (read_id == "0" and not events and not state.get("deferred")
            and not state.get("persist_retry")):
        return 0

    if replay:
        # —— replay 路径：分桶 → 逐窗处理 → 不写快照 ——
        buckets = _replay_bucket(state.get("deferred", []) + events)
        state["deferred"] = []  # replay 不需要 deferred
        state["deferred_ids"] = []
        windows_processed = 0
        total_passengers = 0
        persist_ok = True
        for ws in sorted(buckets):
            result = aggregate_window(
                buckets[ws], ws, WINDOW_SECONDS, LATE_TOLERANCE_WINDOWS,
                ignore_late=True,
            )
            if not persist_window(ws, result):
                persist_ok = False
            total_passengers += result["total"]
            windows_processed += 1
        if persist_ok:
            if msg_ids:
                client.xack(EVENT_STREAM, CONSUMER_GROUP, *msg_ids)
        else:
            # 任一窗口失败都不确认整批，消息留在 pending，重跑回放可补
            log.warning("[REPLAY] 有窗口落库失败，消息保持未确认，可重跑回放")
        log.info(
            "[REPLAY] 处理 %d 个历史窗口，共 %d 客流",
            windows_processed,
            total_passengers,
        )
        # replay 模式不 sleep，尽快把积压历史处理完
        return len(msg_ids)

    # —— 正常路径：now() 做窗口起点 ——
    window_start = window_start_of(datetime.now(TZ), WINDOW_SECONDS)
    window_end = window_start + timedelta(seconds=WINDOW_SECONDS)

    # deferred 里的事件来自上一轮，消息 ID 必须跟着一起留到本轮
    all_events = state.get("deferred", []) + events
    all_ids = state.get("deferred_ids", []) + msg_ids
    ready_idx, deferred_idx = split_ready_indices(all_events, window_end)

    ready = [all_events[i] for i in ready_idx]
    ready_ids = [all_ids[i] for i in ready_idx]
    state["deferred"] = [all_events[i] for i in deferred_idx]
    state["deferred_ids"] = [all_ids[i] for i in deferred_idx]

    if len(state["deferred"]) > MAX_DEFERRED:
        dropped = len(state["deferred"]) - MAX_DEFERRED
        state["deferred"] = state["deferred"][-MAX_DEFERRED:]
        state["deferred_ids"] = state["deferred_ids"][-MAX_DEFERRED:]
        log.warning(
            "deferred 超过 %d 条，丢弃最早 %d 条（消息保持未确认）",
            MAX_DEFERRED,
            dropped,
        )

    _retry_pending(client, state)

    result = aggregate_window(
        ready, window_start, WINDOW_SECONDS, LATE_TOLERANCE_WINDOWS,
        ignore_late=False,
    )
    snapshot = build_snapshot(window_start, result)
    client.set(SNAPSHOT_KEY, json.dumps(snapshot, ensure_ascii=False), ex=SNAPSHOT_TTL)

    warned_before = _persist_warned
    if persist_window(window_start, result):
        if ready_ids:
            client.xack(EVENT_STREAM, CONSUMER_GROUP, *ready_ids)
    else:
        # 不 ACK：消息留在 pending，下轮或重启后还能重试
        state.setdefault("persist_retry", []).append(
            {"ts": window_start, "result": result, "ids": ready_ids}
        )
        if not warned_before:
            log.error(
                "窗口 %s 落库失败，%d 条消息保持未确认并进入重试",
                snapshot["ts"],
                len(ready_ids),
            )

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
    return len(msg_ids)


def drain_pending(client, state):
    """启动时先补消费本消费者遗留的 pending。

    消费者名固定后，重启仍是同一个名字，xreadgroup("0") 才读得到上次
    未确认的消息。遇到 deferred 或落库失败就停，交给主循环处理，
    避免在启动阶段原地打转。
    """
    while _running:
        processed = run_window(client, state, first=True, read_id="0")
        if not processed:
            return
        if state.get("deferred") or state.get("persist_retry"):
            return


def main() -> int:
    parser = argparse.ArgumentParser(description="实时指标消费者")
    parser.add_argument("--once", action="store_true", help="只跑一个窗口，用于自测")
    parser.add_argument(
        "--replay",
        action="store_true",
        help="回填/回放模式：跳过迟到判定，老事件全算进窗口",
    )
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
    state = {
        "name": CONSUMER_NAME,
        "deferred": [],
        "deferred_ids": [],
        "persist_retry": [],
    }
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    log.info("消费者 %s 启动，订阅 %s，窗口 %ss %s",
             state["name"], EVENT_STREAM, WINDOW_SECONDS,
             "[REPLAY]" if args.replay else "")

    try:
        # 正常模式重启后先把上次没确认的消息补完；回放模式不掺 pending
        if not args.replay:
            drain_pending(client, state)
        if args.once:
            run_window(client, state, replay=args.replay, first=True)
            return 0
        while _running:
            run_window(client, state, replay=args.replay)
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
