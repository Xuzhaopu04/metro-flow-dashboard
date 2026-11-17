"""窗口计算的口径测试。

口径以 docs/02-设计/指标口径.md 为准，这里的用例是那份文档的可执行版本。
"""

from datetime import datetime, timedelta, timezone

from stream.metrics import aggregate_window, split_ready, top_stations, window_start_of

TZ = timezone(timedelta(hours=8))
START = datetime(2026, 10, 20, 9, 30, 0, tzinfo=TZ)


def event(station="A01", line="L1", passengers=2, offset_ms=0, event_time=None, event_type="in"):
    return {
        "event_id": "t-1",
        "station_id": station,
        "line_id": line,
        "event_type": event_type,
        "passengers": passengers,
        "event_time": event_time
        or (START + timedelta(milliseconds=offset_ms)).isoformat(timespec="milliseconds"),
    }


def test_empty_window_has_zero_total():
    result = aggregate_window([], START)
    assert result["total"] == 0
    assert result["stations"] == []
    assert result["late_count"] == 0


def test_passengers_summed_per_station_with_line():
    events = [event("A01", passengers=2, offset_ms=100),
              event("A01", passengers=3, offset_ms=300),
              event("B01", "L2", passengers=1, offset_ms=500)]
    result = aggregate_window(events, START)
    assert result["total"] == 6
    # v1.3 加法：stations 每个元素现在带 in_passengers / out_passengers
    assert result["stations"][0] == {
        "station_id": "A01", "line_id": "L1",
        "passengers": 5, "in_passengers": 5, "out_passengers": 0,
    }
    assert result["stations"][1] == {
        "station_id": "B01", "line_id": "L2",
        "passengers": 1, "in_passengers": 1, "out_passengers": 0,
    }


def test_stations_sorted_by_passengers_desc():
    events = [event("B01", "L2", passengers=1), event("A01", passengers=9), event("A02", passengers=4)]
    stations = aggregate_window(events, START)["stations"]
    assert [s["station_id"] for s in stations] == ["A01", "A02", "B01"]


def test_invalid_event_counted_but_not_fatal():
    events = [{"station_id": "A01"}, {"passengers": "abc", "event_time": "2026-10-20T09:30:00.100+08:00"}]
    result = aggregate_window(events, START)
    assert result["total"] == 0
    assert result["invalid_count"] == 2


def test_late_event_goes_to_late_count_only():
    late = (START - timedelta(seconds=3)).isoformat(timespec="milliseconds")
    result = aggregate_window([event(passengers=4, event_time=late)], START)
    assert result["total"] == 0
    assert result["late_count"] == 1
    assert result["late_by_station"] == {"A01": 1}


def test_event_within_tolerance_still_counted():
    recent = (START - timedelta(seconds=1)).isoformat(timespec="milliseconds")
    result = aggregate_window([event(passengers=3, event_time=recent)], START)
    assert result["total"] == 3
    assert result["late_count"] == 0


def test_window_alignment():
    moment = datetime(2026, 10, 20, 9, 30, 7, 830000, tzinfo=TZ)
    assert window_start_of(moment, 1) == datetime(2026, 10, 20, 9, 30, 7, tzinfo=TZ)
    assert window_start_of(moment, 5) == datetime(2026, 10, 20, 9, 30, 5, tzinfo=TZ)


def test_split_ready_defers_next_window_events():
    window_end = START + timedelta(seconds=1)
    ready, deferred = split_ready(
        [event(offset_ms=200), event(offset_ms=1500), {"station_id": "A03"}], window_end
    )
    assert len(ready) == 2
    assert len(deferred) == 1
    assert deferred[0]["event_time"].endswith("01.500+08:00")


def test_top_stations_truncates():
    stations = [{"station_id": f"A0{i}", "passengers": 10 - i} for i in range(1, 7)]
    assert [s["station_id"] for s in top_stations(stations, 2)] == ["A01", "A02"]
    assert top_stations(stations, 0) == []


# ---- 以下三个测试来自"口径一致性约束"和"边界规则"（docs/02-设计/指标口径.md §3-4）----

def test_line_id_missing_defaults_to_empty():
    """指标口径 §3：line_id 缺失时该站 line_id 记为 ""，不影响客流统计。"""
    # 用 hand-crafted event，line_id 字段完全不存在
    no_line_event = {
        "event_id": "t-no-line",
        "station_id": "X01",
        "event_type": "in",
        "passengers": 3,
        "event_time": START.isoformat(timespec="milliseconds"),
    }
    result = aggregate_window([no_line_event], START)
    assert result["total"] == 3
    assert len(result["stations"]) == 1
    # v1.3 加法：stations 带 in/out
    s = result["stations"][0]
    assert s["station_id"] == "X01"
    assert s["line_id"] == ""
    assert s["passengers"] == 3
    assert s["in_passengers"] == 3  # event_type 默认 in
    assert s["out_passengers"] == 0
    # 不能算成非法事件
    assert result["invalid_count"] == 0


def test_line_id_blank_string_or_none_defaults_to_empty():
    """line_id 为空串或 None 时，也要归一成 ""。"""
    blank = {
        "event_id": "t-blank", "station_id": "X02", "line_id": "",
        "event_type": "in", "passengers": 1,
        "event_time": START.isoformat(timespec="milliseconds"),
    }
    none_val = {
        "event_id": "t-none", "station_id": "X03", "line_id": None,
        "event_type": "in", "passengers": 1,
        "event_time": START.isoformat(timespec="milliseconds"),
    }
    result = aggregate_window([blank, none_val], START)
    # 两个站都应该出现，line_id 都是 ""
    assert len(result["stations"]) == 2
    for s in result["stations"]:
        assert s["line_id"] == ""


def test_total_equals_sum_of_stations_passengers():
    """指标口径 §4 一致性约束：同一窗口内 total == sum(stations[].passengers)。"""
    events = [
        event("A01", passengers=2, offset_ms=100),
        event("A01", passengers=3, offset_ms=200),
        event("B01", "L2", passengers=1, offset_ms=300),
        event("C01", "L3", passengers=4, offset_ms=400),
        # 混入一个迟到事件，确认不会算进 total 也不会进 stations
    ]
    late = (START - timedelta(seconds=5)).isoformat(timespec="milliseconds")
    events.append(event("A01", passengers=99, event_time=late))

    result = aggregate_window(events, START)
    assert result["total"] == sum(s["passengers"] for s in result["stations"])
    assert result["total"] == 10  # 2+3+1+4，迟到的 99 不算
    assert result["late_count"] == 1


def test_result_structure_matches_contract_v1_1():
    """契约 v1.1 + v1.3 加法：聚合结果要能被 build_snapshot 直接序列化成快照。

    v1.3 新增 in_total/out_total（顶层）和 stations 里的 in/out 拆分字段；
    快照本身暂不写这几个新字段，但 result 必须有，供 persist_window 和服务端用。
    """
    result = aggregate_window([event()], START)
    # 顶层键：v1.1 五个 + v1.3 新增的 in_total / out_total + 内部 late_by_station
    expected_keys = {
        "total", "in_total", "out_total",
        "stations", "late_count", "invalid_count", "late_by_station",
    }
    assert set(result.keys()) == expected_keys
    # stations 内每个元素：v1.1 三个 + v1.3 新增 in/out
    for s in result["stations"]:
        assert set(s.keys()) == {
            "station_id", "line_id", "passengers",
            "in_passengers", "out_passengers",
        }
    # 数值类型必须是 int，不能是 str / float
    assert isinstance(result["total"], int)
    assert isinstance(result["in_total"], int)
    assert isinstance(result["out_total"], int)
    assert isinstance(result["late_count"], int)
    assert isinstance(result["invalid_count"], int)
    for s in result["stations"]:
        assert isinstance(s["passengers"], int)
        assert isinstance(s["in_passengers"], int)
        assert isinstance(s["out_passengers"], int)
    # 空窗口时 stations 是空 list，in_total/out_total 也必须是 0
    empty = aggregate_window([], START)
    assert empty["stations"] == []
    assert empty["in_total"] == 0
    assert empty["out_total"] == 0


# ---- v1.3 契约新增：进/出站拆分 ----

def test_v13_in_plus_out_equals_total():
    """契约 v1.3 口径保证：passengers（乘降量）= in_passengers + out_passengers。"""
    events = [
        event("A01", passengers=2, offset_ms=100, event_type="in"),
        event("A01", passengers=3, offset_ms=200, event_type="in"),
        event("A01", passengers=4, offset_ms=300, event_type="out"),
    ]
    result = aggregate_window(events, START)
    # 顶层一致性
    assert result["total"] == 2 + 3 + 4  # 9
    assert result["in_total"] == 5       # 2 + 3
    assert result["out_total"] == 4
    assert result["in_total"] + result["out_total"] == result["total"]
    # 按站一致性
    for s in result["stations"]:
        assert s["in_passengers"] + s["out_passengers"] == s["passengers"]


def test_v13_event_type_out_counts_as_out():
    """event_type=out 的事件必须进 out_passengers，不能进 in_passengers。"""
    events = [
        event("A01", passengers=2, offset_ms=100, event_type="in"),
        event("A01", passengers=3, offset_ms=200, event_type="out"),
        event("A01", passengers=1, offset_ms=300, event_type="out"),
    ]
    result = aggregate_window(events, START)
    s = result["stations"][0]
    assert s["passengers"] == 6
    assert s["in_passengers"] == 2
    assert s["out_passengers"] == 4
    assert result["in_total"] == 2
    assert result["out_total"] == 4


def test_v13_missing_event_type_defaults_to_in():
    """兼容：缺 event_type 字段的历史事件按 in 处理，不计为非法。"""
    e = {
        "event_id": "t-old",
        "station_id": "Y01",
        "line_id": "L9",
        "passengers": 5,
        "event_time": START.isoformat(timespec="milliseconds"),
        # 故意不写 event_type
    }
    result = aggregate_window([e], START)
    assert result["invalid_count"] == 0
    assert result["total"] == 5
    s = result["stations"][0]
    assert s["in_passengers"] == 5
    assert s["out_passengers"] == 0


def test_v13_empty_window_in_out_zero():
    """空窗口时 in_total/out_total 必须显式为 0，不能丢字段。"""
    result = aggregate_window([], START)
    assert result["in_total"] == 0
    assert result["out_total"] == 0


def test_replay_mode_skips_late_check():
    """replay/backfill 模式下，本该判迟到的老事件也要算进窗口。

    场景：窗口 START，事件比窗口早 10 秒（远超过 late_tolerance=2s）：
    - 默认模式 → 迟到跳过，total=0，late_count=1
    - replay（ignore_late=True）→ 算进窗口，total=7，late_count=0
    """
    late_event = event(station="A01", line="L1", passengers=7, offset_ms=-10000, event_type="in")

    # 默认模式：迟到跳过（late_tolerance 默认 2 窗口 = 2s，事件早 10s 远超）
    default = aggregate_window([late_event], START)
    assert default["total"] == 0
    assert default["late_count"] == 1
    assert default["stations"] == []

    # replay 模式：跳过迟到判定，算进窗口
    replayed = aggregate_window([late_event], START, ignore_late=True)
    assert replayed["total"] == 7
    assert replayed["in_total"] == 7
    assert replayed["out_total"] == 0
    assert replayed["late_count"] == 0
    assert len(replayed["stations"]) == 1
    assert replayed["stations"][0]["station_id"] == "A01"
    assert replayed["stations"][0]["passengers"] == 7


def test_replay_mode_normal_events_still_work():
    """replay 模式不影响正常事件（本来就在窗口内的）。"""
    normal = event(station="B02", line="L2", passengers=3, offset_ms=0)
    result = aggregate_window([normal], START, ignore_late=True)
    assert result["total"] == 3
    assert result["late_count"] == 0
    assert result["stations"][0]["passengers"] == 3
