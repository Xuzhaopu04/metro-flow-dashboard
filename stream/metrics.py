"""窗口指标计算。

这里的函数都是纯函数：不连 Redis、不读系统时间，窗口起点由调用方传进来。
只有这样才能脱离中间件写单元测试，也才能复现"某一秒到底算出来多少"。

口径以 docs/02-设计/指标口径.md 为准，改这里之前先改那份文档。
"""

from collections import defaultdict
from datetime import datetime, timedelta


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def window_start_of(moment: datetime, window_seconds: int = 1) -> datetime:
    """把时刻向下对齐到窗口边界，避免快照时间戳每轮抖动。"""
    epoch = int(moment.timestamp())
    return datetime.fromtimestamp(epoch - epoch % window_seconds, tz=moment.tzinfo)


def split_ready(events, window_end: datetime):
    """按事件时间把一批事件分成"本轮能算的"和"属于后面窗口的"。

    一次拉取可能带回跨越多个窗口的事件，全塞进当前窗口会让某一秒的客流虚高。
    解析不了的事件留在 ready 里，交给 aggregate_window 记成非法。
    """
    ready, deferred = [], []
    for event in events:
        try:
            event_time = parse_time(event["event_time"])
        except (KeyError, TypeError, ValueError):
            ready.append(event)
            continue
        (deferred if event_time >= window_end else ready).append(event)

    return ready, deferred


def aggregate_window(
    events,
    window_start: datetime,
    window_seconds: int = 1,
    late_tolerance_windows: int = 2,
    ignore_late: bool = False,
) -> dict:
    """把一个窗口内的事件聚合成指标。

    ignore_late=True 用于 replay/backfill 模式：
    历史事件本应被判迟到跳过，但回放场景需要完整算出。
    正常消费（实时流）保持 ignore_late=False，迟到容忍 = late_tolerance_windows。
    """
    window_end = window_start + timedelta(seconds=window_seconds)
    late_threshold = window_start - timedelta(seconds=window_seconds * late_tolerance_windows)

    passengers = defaultdict(int)       # 乘降量（in + out），保留 v1.2 语义
    in_pass = defaultdict(int)          # 进站量，Slice 1 新增
    out_pass = defaultdict(int)         # 出站量，Slice 1 新增
    station_line = {}
    late_by_station = defaultdict(int)
    late_count = 0
    invalid_count = 0

    for event in events:
        try:
            event_time = parse_time(event["event_time"])
            count = int(event["passengers"])
            station_id = str(event["station_id"])
            event_type = str(event.get("event_type") or "in").lower()
        except (KeyError, TypeError, ValueError):
            invalid_count += 1
            continue

        # replay/backfill 模式跳过迟到判定：事件全算进窗口
        if not ignore_late and event_time < late_threshold:
            late_count += 1
            late_by_station[station_id] += 1
            continue
        if event_time >= window_end:
            # 正常流程已被 split_ready 拦住，这里只是兜底
            continue

        passengers[station_id] += count
        # v1.3 加法：按 event_type 拆分进站/出站，缺省按 in 处理（历史事件兼容）
        if event_type == "out":
            out_pass[station_id] += count
        else:
            in_pass[station_id] += count
        station_line.setdefault(station_id, event.get("line_id") or "")

    stations = [
        {
            "station_id": sid,
            "line_id": station_line[sid],
            "passengers": n,
            "in_passengers": in_pass[sid],
            "out_passengers": out_pass[sid],
        }
        for sid, n in sorted(passengers.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return {
        "total": sum(passengers.values()),
        "in_total": sum(in_pass.values()),
        "out_total": sum(out_pass.values()),
        "stations": stations,
        "late_count": late_count,
        "invalid_count": invalid_count,
        "late_by_station": dict(late_by_station),
    }


def top_stations(stations, top: int = 5):
    """取客流前 N 名，入参已是降序，这里只做截断，顺带保证不越界。"""
    return list(stations[: max(top, 0)])
