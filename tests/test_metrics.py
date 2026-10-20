"""窗口计算的口径测试。

口径以 docs/02-设计/指标口径.md 为准，这里的用例是那份文档的可执行版本。
"""

from datetime import datetime, timedelta, timezone

from stream.metrics import aggregate_window, split_ready, top_stations, window_start_of

TZ = timezone(timedelta(hours=8))
START = datetime(2026, 10, 20, 9, 30, 0, tzinfo=TZ)


def event(station="A01", line="L1", passengers=2, offset_ms=0, event_time=None):
    return {
        "event_id": "t-1",
        "station_id": station,
        "line_id": line,
        "event_type": "in",
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
    assert result["stations"][0] == {"station_id": "A01", "line_id": "L1", "passengers": 5}
    assert result["stations"][1] == {"station_id": "B01", "line_id": "L2", "passengers": 1}


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
