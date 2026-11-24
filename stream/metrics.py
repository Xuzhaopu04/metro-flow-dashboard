"""窗口指标计算。

这里的函数都是纯函数：不连 Redis、不读系统时间，窗口起点由调用方传进来。
只有这样才能脱离中间件写单元测试，也才能复现"某一秒到底算出来多少"。

口径以 docs/02-设计/指标口径.md 为准，改这里之前先改那份文档。
"""

from collections import defaultdict
from datetime import datetime, timedelta

# 防御性上限：契约给的是 1—5，但真实闸机可能合批；这里只挡住会撑爆
# MySQL INT / varchar(16) 的异常值，避免一条毒消息让整个窗口永远落不了库。
MAX_PASSENGERS = 1_000_000
MAX_STATION_ID_LEN = 16


def parse_time(value: str) -> datetime:
    """解析事件时间，强制要求带时区。

    契约 2.1 要求 event_time 带时区。缺失时区的时间如果放过去，
    后面与窗口边界比较会抛 TypeError，整条消费链路就退出了；
    这里提前当成解析失败，由调用方记非法事件。
    """
    if not isinstance(value, str):
        raise TypeError("event_time 必须是字符串")
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("event_time 必须带时区")
    return moment


def window_start_of(moment: datetime, window_seconds: int = 1) -> datetime:
    """把时刻向下对齐到窗口边界，避免快照时间戳每轮抖动。"""
    epoch = int(moment.timestamp())
    return datetime.fromtimestamp(epoch - epoch % window_seconds, tz=moment.tzinfo)


def split_ready_indices(events, window_end: datetime):
    """按事件时间把下标分成"本轮能算的"和"属于后面窗口的"。

    消费端要按消息 ID 确认，必须保留原始下标；split_ready 只返回事件，
    两边各写一套判断迟早会分叉，所以让 split_ready 复用这里的实现。
    """
    ready, deferred = [], []
    for index, event in enumerate(events):
        try:
            event_time = parse_time(event["event_time"])
        except (KeyError, TypeError, ValueError):
            ready.append(index)
            continue
        (deferred if event_time >= window_end else ready).append(index)
    return ready, deferred


def split_ready(events, window_end: datetime):
    """按事件时间把一批事件分成"本轮能算的"和"属于后面窗口的"。

    一次拉取可能带回跨越多个窗口的事件，全塞进当前窗口会让某一秒的客流虚高。
    解析不了的事件留在 ready 里，交给 aggregate_window 记成非法。
    """
    ready_idx, deferred_idx = split_ready_indices(events, window_end)
    return [events[i] for i in ready_idx], [events[i] for i in deferred_idx]


def _validate_event(event):
    """校验必填字段，返回 (event_time, station_id, count)。

    口径要求整数客流、非空站点、带时区时间；不做隐式转换。
    `int("3")`、`int(3.9)`、`int(True)` 都会把脏数据洗成合法数，
    这正是最危险的一类错误：算出来看着对，口径已经错了。
    """
    event_id = event["event_id"]
    station_id = event["station_id"]
    raw_count = event["passengers"]
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("event_id")
    if not isinstance(station_id, str) or not station_id.strip():
        raise ValueError("station_id")
    if len(station_id.strip()) > MAX_STATION_ID_LEN:
        raise ValueError("station_id")
    if isinstance(raw_count, bool) or not isinstance(raw_count, int):
        raise ValueError("passengers")
    if raw_count < 0 or raw_count > MAX_PASSENGERS:
        raise ValueError("passengers")
    return parse_time(event["event_time"]), station_id.strip(), raw_count


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
            event_time, station_id, count = _validate_event(event)
        except (KeyError, TypeError, ValueError):
            invalid_count += 1
            continue

        # v1.3：event_type 缺失或不是 in/out 一律算非法，不静默归到某个方向
        raw_type = event.get("event_type")
        event_type = str(raw_type).strip().lower() if raw_type is not None else ""
        if event_type not in ("in", "out"):
            invalid_count += 1
            continue

        # replay/backfill 模式跳过迟到判定：事件全算进窗口
        # 边界取等号：文档写"早 2 秒以上"算迟到，正好 2 秒也应计入迟到
        if not ignore_late and event_time <= late_threshold:
            late_count += 1
            late_by_station[station_id] += 1
            continue
        if event_time >= window_end:
            # 正常流程已被 split_ready 拦住，这里只是兜底
            continue

        passengers[station_id] += count
        if event_type == "out":
            out_pass[station_id] += count
        else:
            in_pass[station_id] += count
        raw_line = event.get("line_id")
        station_line.setdefault(
            station_id, raw_line.strip() if isinstance(raw_line, str) else ""
        )

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
