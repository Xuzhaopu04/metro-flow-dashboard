"""接口层测试：参数校验、空数据降级、404 结构。

用 config.settings_test 里的 SQLite 与失效的 Redis 端口，不需要外部依赖。
"""

import json
from datetime import timedelta

import pytest
from django.conf import settings
from django.db import DatabaseError
from django.utils import timezone

from api import views
from api.models import MetricQuality, MetricWindow


class FakeRedis:
    def __init__(self, payload=None):
        self.payload = payload

    def get(self, key):
        return self.payload


def make_rows(station_id, base, count=3, passengers=5, in_p=None, out_p=None,
              late=0):
    """造 count 行，时间从 base 起每秒一行。base 由调用方给定，避免各站时间戳错位。

    v1.3 起可以指定进/出站分项；不指定时按 passengers 对半分，
    保证 total == in + out 这个约束在测试数据里也成立。
    late 用于构造迟到数据，供 quality 的回退分支验证。
    """
    if in_p is None:
        in_p = passengers // 2
    if out_p is None:
        out_p = passengers - in_p
    for i in range(count):
        MetricWindow.objects.create(
            ts=base + timedelta(seconds=i),
            station_id=station_id,
            passengers=passengers,
            in_passengers=in_p,
            out_passengers=out_p,
            late_count=late,
        )


@pytest.fixture
def base_ts():
    return (timezone.now() - timedelta(minutes=1)).replace(microsecond=0)


@pytest.fixture
def fake_redis(monkeypatch):
    def install(payload):
        monkeypatch.setattr(views, "_client", FakeRedis(payload))

    return install


def test_snapshot_falls_back_when_redis_missing(client):
    response = client.get("/api/snapshot")
    assert response.status_code == 200
    body = response.json()
    assert body["ts"] is None and body["total"] == 0 and body["stations"] == []
    assert response["Cache-Control"] == "no-store"


def test_snapshot_returns_redis_payload(client, fake_redis):
    payload = {"ts": "2026-10-20T09:30:15+08:00", "total": 37,
               "stations": [{"station_id": "A01", "line_id": "L1", "passengers": 12}],
               "late_count": 1, "invalid_count": 0}
    fake_redis(json.dumps(payload))
    body = client.get("/api/snapshot").json()
    # Redis 里存的是 5 个字段，接口层要原样带出来，不多不少
    for key, value in payload.items():
        assert body[key] == value


def test_snapshot_adds_alert_fields(client, fake_redis):
    """快照响应必须带 alerting 与 threshold，契约 4.1。"""
    fake_redis(json.dumps({"ts": None, "total": 1, "stations": [],
                           "late_count": 0, "invalid_count": 0}))
    body = client.get("/api/snapshot").json()
    assert set(body) == {"ts", "total", "stations", "late_count",
                         "invalid_count", "alerting", "threshold"}
    assert body["alerting"] is False
    assert body["threshold"] == settings.ALERT_THRESHOLD


def test_snapshot_alerts_when_total_reaches_threshold(client, fake_redis, monkeypatch):
    """恰好等于阈值也要告警（>= 而非 >）。"""
    monkeypatch.setattr(settings, "ALERT_THRESHOLD", 100)
    fake_redis(json.dumps({"ts": None, "total": 100, "stations": [],
                           "late_count": 0, "invalid_count": 0}))
    assert client.get("/api/snapshot").json()["alerting"] is True


def test_snapshot_does_not_alert_below_threshold(client, fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "ALERT_THRESHOLD", 100)
    fake_redis(json.dumps({"ts": None, "total": 99, "stations": [],
                           "late_count": 0, "invalid_count": 0}))
    assert client.get("/api/snapshot").json()["alerting"] is False


def test_alert_follows_live_config_not_snapshot(client, fake_redis, monkeypatch):
    """阈值改了要立刻生效，不能沿用写快照时的旧判断。

    消费者根本不知道阈值，所以 alerting 只能由接口层现算。
    """
    payload = {"ts": None, "total": 50, "stations": [],
               "late_count": 0, "invalid_count": 0}
    fake_redis(json.dumps(payload))
    monkeypatch.setattr(settings, "ALERT_THRESHOLD", 100)
    assert client.get("/api/snapshot").json()["alerting"] is False
    monkeypatch.setattr(settings, "ALERT_THRESHOLD", 10)
    body = client.get("/api/snapshot").json()
    assert body["alerting"] is True
    assert body["threshold"] == 10


def test_empty_snapshot_has_alert_fields(client):
    """Redis 里没数据时同样要给告警字段，前端不该在这里分支。"""
    body = client.get("/api/snapshot").json()
    assert body["alerting"] is False
    assert body["threshold"] == settings.ALERT_THRESHOLD


def test_normalize_snapshot_ignores_upstream_alert_fields():
    """上游若擅自写了 alerting，以服务端现算的为准。"""
    dirty = {"total": 5, "alerting": True, "threshold": 1}
    result = views.normalize_snapshot(dirty)
    assert result["alerting"] is False
    assert result["threshold"] == settings.ALERT_THRESHOLD


def test_snapshot_falls_back_on_broken_json(client, fake_redis):
    fake_redis("{坏的快照")
    assert client.get("/api/snapshot").json()["total"] == 0


@pytest.mark.django_db
def test_series_aggregates_all_stations(client, base_ts):
    make_rows("A01", base_ts, passengers=2)
    make_rows("B01", base_ts, passengers=3)
    body = client.get("/api/series?minutes=5").json()
    assert body["station_id"] is None
    assert len(body["points"]) == 3
    assert body["points"][0]["passengers"] == 5


@pytest.mark.django_db
def test_series_filters_by_station(client, base_ts):
    make_rows("A01", base_ts, count=2, passengers=4)
    make_rows("B01", base_ts, count=2, passengers=9)
    body = client.get("/api/series?station_id=A01&minutes=5").json()
    assert body["station_id"] == "A01"
    assert all(p["passengers"] == 4 for p in body["points"])


@pytest.mark.django_db
def test_series_ignores_rows_outside_window(client):
    make_rows("A01", timezone.now() - timedelta(minutes=30), count=2)
    body = client.get("/api/series?minutes=5").json()
    assert body["points"] == []


@pytest.mark.parametrize("query", ["minutes=abc", "minutes=0", "minutes=61"])
def test_series_rejects_bad_minutes(client, query):
    response = client.get(f"/api/series?{query}")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_PARAM"


@pytest.mark.django_db
def test_rank_returns_top_n_sorted(client, base_ts):
    make_rows("A01", base_ts, count=1, passengers=3)
    make_rows("B01", base_ts, count=1, passengers=10)
    make_rows("A02", base_ts, count=1, passengers=6)
    body = client.get("/api/rank?top=2").json()
    assert [row["station_id"] for row in body["top"]] == ["B01", "A02"]
    assert body["top"][0]["passengers"] == 10


@pytest.mark.django_db
def test_rank_default_top_is_five(client, base_ts):
    for i in range(1, 8):
        make_rows(f"A0{i}", base_ts, count=1, passengers=i)
    assert len(client.get("/api/rank").json()["top"]) == 5


@pytest.mark.parametrize("query", ["top=0", "top=99", "top=x"])
def test_rank_rejects_bad_top(client, query):
    response = client.get(f"/api/rank?{query}")
    assert response.status_code == 400


def test_unknown_path_returns_json_404(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_dashboard_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "实时客流" in response.content.decode()


# ---- 数据库故障降级 ----
# 架构说明第 3 节：MySQL 挂了只该让历史与排行查不到，实时快照仍可用。
# 不降级的话页面会拿到 500 甚至 HTML 报错页，把"库在维护"变成"系统坏了"。

def break_db(monkeypatch):
    """让任意 ORM 查询在求值时抛 DatabaseError，模拟 MySQL 不可用。"""
    def boom(*args, **kwargs):
        raise DatabaseError("simulated: MySQL is down")

    monkeypatch.setattr(views.MetricWindow.objects, "filter", boom)


@pytest.mark.django_db
def test_series_degrades_to_empty_points_when_db_down(client, monkeypatch):
    break_db(monkeypatch)
    response = client.get("/api/series?minutes=5")
    assert response.status_code == 200
    body = response.json()
    assert body["points"] == []
    # 契约 4.2 v1.3 的字段白名单；降级时同样要给全，前端不该在这里分支
    assert set(body) == {"station_id", "line_id", "direction",
                         "granularity", "truncated", "points"}
    assert body["truncated"] is False


@pytest.mark.django_db
def test_rank_degrades_to_empty_top_when_db_down(client, monkeypatch):
    break_db(monkeypatch)
    response = client.get("/api/rank?top=5")
    assert response.status_code == 200
    assert response.json() == {"top": []}


@pytest.mark.django_db
def test_snapshot_still_works_when_db_down(client, monkeypatch, fake_redis):
    """降级只影响查库的两个接口，快照走 Redis，不该被牵连。"""
    break_db(monkeypatch)
    fake_redis(json.dumps({"ts": "2026-10-20T09:30:15+08:00", "total": 3,
                           "stations": [], "late_count": 0, "invalid_count": 0}))
    response = client.get("/api/snapshot")
    assert response.status_code == 200
    assert response.json()["total"] == 3


@pytest.mark.django_db
def test_db_degradation_is_logged(client, monkeypatch, caplog):
    """降级必须留日志，否则排障时看不出数据是"本来没有"还是"取不到"。"""
    break_db(monkeypatch)
    with caplog.at_level("WARNING", logger="api.views"):
        client.get("/api/series")
    assert any("降级" in record.message for record in caplog.records)


@pytest.mark.django_db
def test_db_errors_are_not_swallowed_silently(client, monkeypatch):
    """只降级 DatabaseError；编程错误要继续暴露，不能被 except 吞掉。"""
    def boom(*args, **kwargs):
        raise AttributeError("simulated: 字段名写错")

    monkeypatch.setattr(views.MetricWindow.objects, "filter", boom)
    with pytest.raises(AttributeError):
        client.get("/api/series")


# ---- 契约 v1.3：分方向、分线路、聚合粒度、今日汇总、站点元数据 ----

@pytest.fixture
def station_file(tmp_path, monkeypatch):
    """把站点表指到临时文件，避免测试依赖仓库里那份的真实内容。"""
    path = tmp_path / "stations.json"
    monkeypatch.setattr("api.stations.stations_file", lambda: str(path))

    def write(payload):
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    return write


@pytest.mark.django_db
def test_series_point_has_in_out_total(client, base_ts):
    """契约 4.2：点结构同时给 in/out/total，并保留 passengers 供旧前端读。"""
    make_rows("A01", base_ts, count=1, passengers=10, in_p=6, out_p=4)
    point = client.get("/api/series?minutes=5").json()["points"][0]
    assert point["in"] == 6 and point["out"] == 4
    assert point["total"] == 10
    assert point["passengers"] == 10   # 兼容 v1.2


@pytest.mark.django_db
def test_series_direction_keeps_total(client, base_ts):
    """切方向只影响分项，total 不变——否则总量口径会随筛选跳变。"""
    make_rows("A01", base_ts, count=1, passengers=10, in_p=7, out_p=3)
    only_in = client.get("/api/series?direction=in").json()["points"][0]
    assert only_in["in"] == 7 and only_in["out"] == 0
    assert only_in["total"] == 7


def test_series_rejects_bad_direction(client):
    response = client.get("/api/series?direction=up")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_PARAM"


def test_series_rejects_bad_granularity(client):
    response = client.get("/api/series?granularity=week")
    assert response.status_code == 400


@pytest.mark.django_db
def test_series_granularity_minute_buckets(client, base_ts):
    """同一分钟内的多点应合成一个桶。"""
    make_rows("A01", base_ts.replace(second=0), count=5, passengers=2)
    points = client.get("/api/series?granularity=minute").json()["points"]
    assert len(points) == 1
    assert points[0]["total"] == 10


@pytest.mark.django_db
def test_series_filters_by_line(client, base_ts, station_file):
    station_file({"A01": {"line_id": "L1"}, "B01": {"line_id": "L2"}})
    make_rows("A01", base_ts, count=1, passengers=5)
    make_rows("B01", base_ts, count=1, passengers=9)
    body = client.get("/api/series?line_id=L1").json()
    assert body["line_id"] == "L1"
    assert body["points"][0]["total"] == 5


@pytest.mark.django_db
def test_series_unknown_line_returns_empty(client, base_ts, station_file):
    """线路筛不出站点时给空序列，不能退化成"没筛"返回全量。"""
    station_file({"A01": {"line_id": "L1"}})
    make_rows("A01", base_ts, count=1, passengers=5)
    assert client.get("/api/series?line_id=L9").json()["points"] == []


@pytest.mark.django_db
def test_series_from_to_overrides_minutes(client, base_ts):
    make_rows("A01", base_ts, count=3, passengers=5)
    start = base_ts.isoformat()
    end = (base_ts + timedelta(seconds=10)).isoformat()
    body = client.get(f"/api/series?from={start}&to={end}&minutes=60").json()
    assert len(body["points"]) == 3


@pytest.mark.django_db
def test_series_accepts_unencoded_plus_offset(client, base_ts):
    """时区里的 + 不编码会被查询串解析成空格，服务端要兜住这种最常见的写法。"""
    make_rows("A01", base_ts, count=1, passengers=5)
    assert "+" in base_ts.isoformat()
    body = client.get(f"/api/series?from={base_ts.isoformat()}").json()
    assert len(body["points"]) == 1


def test_series_rejects_bad_time(client):
    assert client.get("/api/series?from=昨天").status_code == 400


def test_series_rejects_reversed_range(client):
    now = timezone.now()
    start = (now + timedelta(hours=1)).isoformat()
    end = now.isoformat()
    assert client.get(f"/api/series?from={start}&to={end}").status_code == 400


def test_series_rejects_oversized_span(client):
    now = timezone.now()
    start = (now - timedelta(hours=25)).isoformat()
    assert client.get(f"/api/series?from={start}&to={now.isoformat()}").status_code == 400


def test_thin_points_keeps_first_and_last():
    """抽稀要保留首尾，否则曲线两端被削掉，看不出起点和当前值。"""
    points = [{"ts": str(i)} for i in range(5000)]
    kept, truncated = views.thin_points(points, limit=100)
    assert truncated is True and len(kept) == 100
    assert kept[0] == points[0] and kept[-1] == points[-1]


@pytest.mark.django_db
def test_rank_accepts_minutes(client, base_ts):
    make_rows("A01", base_ts, count=1, passengers=5)
    assert client.get("/api/rank?minutes=30").status_code == 200


@pytest.mark.django_db
def test_rank_direction_orders_by_that_direction(client, base_ts, station_file):
    """切到进站榜时，名次要按进站量排，不能还按总量。"""
    station_file({"A01": {"line_id": "L1"}, "B01": {"line_id": "L1"}})
    make_rows("A01", base_ts, count=1, passengers=10, in_p=9, out_p=1)
    make_rows("B01", base_ts, count=1, passengers=10, in_p=1, out_p=9)
    body = client.get("/api/rank?direction=in").json()
    assert [r["station_id"] for r in body["top"]] == ["A01", "B01"]


@pytest.mark.parametrize("query", ["minutes=0", "minutes=61", "direction=up"])
def test_rank_rejects_bad_new_params(client, query):
    assert client.get(f"/api/rank?{query}").status_code == 400


@pytest.mark.django_db
def test_summary_today_shape(client, base_ts):
    make_rows("A01", base_ts, count=2, passengers=6, in_p=4, out_p=2)
    body = client.get("/api/summary/today").json()
    assert set(body) == {"date", "since", "updated_at", "total", "in", "out",
                         "net", "peak", "lines", "top_stations", "quality"}
    assert body["total"] == body["in"] + body["out"]
    assert body["net"] == body["in"] - body["out"]


@pytest.mark.django_db
def test_summary_today_starts_at_midnight(client):
    """契约 4.4：起点是当日 0 点，不是最近 24 小时。"""
    body = client.get("/api/summary/today").json()
    assert body["since"].endswith("00:00:00+08:00")
    assert body["date"] == timezone.localtime(timezone.now()).strftime("%Y-%m-%d")


@pytest.mark.django_db
def test_summary_today_empty_is_zeroed(client):
    """无数据也要给完整结构，数值 0、peak.ts 为 null，不能 404。"""
    body = client.get("/api/summary/today").json()
    assert body["total"] == 0 and body["lines"] == []
    assert body["peak"] == {"ts": None, "total": 0}


@pytest.mark.django_db
def test_summary_today_lines_use_station_map(client, base_ts, station_file):
    station_file({"A01": {"line_id": "L1"}, "B01": {"line_id": "L2"}})
    make_rows("A01", base_ts, count=1, passengers=4)
    make_rows("B01", base_ts, count=1, passengers=9)
    lines = {item["line_id"]: item["total"]
             for item in client.get("/api/summary/today").json()["lines"]}
    assert lines == {"L1": 4, "L2": 9}


@pytest.mark.django_db
def test_summary_today_top_stations_capped(client, base_ts, station_file):
    station_file({f"A{i:02d}": {"line_id": "L1"} for i in range(1, 9)})
    for i in range(1, 9):
        make_rows(f"A{i:02d}", base_ts, count=1, passengers=i)
    top = client.get("/api/summary/today").json()["top_stations"]
    assert len(top) == 5
    assert top[0]["station_id"] == "A08"   # 客流最大


@pytest.mark.django_db
def test_summary_today_degrades_when_db_down(client, monkeypatch):
    break_db(monkeypatch)
    response = client.get("/api/summary/today")
    assert response.status_code == 200
    assert response.json()["total"] == 0


@pytest.mark.django_db
def test_summary_quality_has_invalid_count(client):
    """契约 4.4 要求 quality 三个字段齐全，缺 invalid_count 前端会显示 undefined。"""
    body = client.get("/api/summary/today").json()
    assert set(body["quality"]) == {"late_count", "invalid_count", "rows"}


@pytest.mark.django_db
def test_summary_quality_reads_quality_table(client, base_ts):
    """有窗口级质量数据时以它为准，不被 metric_window 的迟到数覆盖。"""
    MetricQuality.objects.create(ts=base_ts, late_count=7, invalid_count=4)
    make_rows("A01", base_ts, count=1, passengers=5, late=1)
    quality = client.get("/api/summary/today").json()["quality"]
    assert quality["late_count"] == 7
    assert quality["invalid_count"] == 4


@pytest.mark.django_db
def test_summary_quality_falls_back_to_window_late(client, base_ts):
    """过渡期新表为空：迟到回退到逐站累加，非法只能给 0。"""
    make_rows("A01", base_ts, count=1, passengers=5, late=2)
    make_rows("B01", base_ts, count=1, passengers=5, late=3)
    quality = client.get("/api/summary/today").json()["quality"]
    assert quality["late_count"] == 5      # 2 + 3，两站各自迟到数
    assert quality["invalid_count"] == 0   # 无站点级来源可回退
    assert quality["rows"] == 2


@pytest.mark.django_db
def test_summary_quality_degrades_when_db_down(client, monkeypatch):
    """数据库挂掉时 quality 也要给 0 值结构，不能让整个接口变 500。"""
    break_db(monkeypatch)
    quality = client.get("/api/summary/today").json()["quality"]
    assert quality == {"late_count": 0, "invalid_count": 0, "rows": 0}


def test_stations_reads_collector_file(client, station_file):
    station_file({
        "A01": {"line_id": "L1", "name": "人民广场", "order": 1, "lng": 121.47, "lat": 31.23},
        "B01": {"line_id": "L2"},
    })
    body = client.get("/api/stations").json()
    assert set(body) == {"stations", "lines"}
    a01 = next(s for s in body["stations"] if s["station_id"] == "A01")
    assert a01["name"] == "人民广场" and a01["lng"] == 121.47
    # 采集表没给名称时返回 null，不编造
    b01 = next(s for s in body["stations"] if s["station_id"] == "B01")
    assert b01["name"] is None and b01["order"] is None


def test_stations_groups_lines(client, station_file):
    station_file({"A01": {"line_id": "L1"}, "A02": {"line_id": "L1"},
                  "B01": {"line_id": "L2"}})
    body = client.get("/api/stations").json()
    lines = {line["line_id"]: line["station_ids"] for line in body["lines"]}
    assert lines == {"L1": ["A01", "A02"], "L2": ["B01"]}


def test_stations_missing_file_returns_empty(client, tmp_path, monkeypatch):
    """站点表丢了只该影响这个接口，返回空列表并留日志，不能 500。"""
    missing = tmp_path / "nope.json"
    monkeypatch.setattr("api.stations.stations_file", lambda: str(missing))
    body = client.get("/api/stations").json()
    assert body == {"stations": [], "lines": []}


def test_stations_broken_json_returns_empty(client, tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{不是 JSON", encoding="utf-8")
    monkeypatch.setattr("api.stations.stations_file", lambda: str(bad))
    assert client.get("/api/stations").json() == {"stations": [], "lines": []}


@pytest.mark.django_db
def test_old_series_call_still_works(client, base_ts):
    """契约承诺"旧调用不传参不受影响"：v1.2 的请求原样可用。"""
    make_rows("A01", base_ts, count=2, passengers=5)
    body = client.get("/api/series?station_id=A01&minutes=5").json()
    assert body["station_id"] == "A01"
    assert len(body["points"]) == 2
    assert body["granularity"] == "second" and body["direction"] == "all"


@pytest.mark.django_db
def test_rank_in_direction_is_sorted_by_in(client, base_ts, station_file):
    """进站方向必须按进站量降序。in/out 列全 0 时曾按 0 排序导致名次与数字矛盾。"""
    station_file({"A01": {"line_id": "L1"}, "B01": {"line_id": "L1"},
                  "C01": {"line_id": "L1"}})
    make_rows("A01", base_ts, count=1, passengers=300, in_p=290, out_p=10)
    make_rows("B01", base_ts, count=1, passengers=233, in_p=233, out_p=0)
    make_rows("C01", base_ts, count=1, passengers=273, in_p=273, out_p=0)
    top = client.get("/api/rank?direction=in&top=3").json()["top"]
    values = [row["in"] for row in top]
    assert values == sorted(values, reverse=True)
    assert [row["station_id"] for row in top] == ["A01", "C01", "B01"]


@pytest.mark.django_db
def test_summary_today_times_are_local(client):
    """since 与 updated_at 都用项目时区，前端算延迟才不会差 8 小时。"""
    body = client.get("/api/summary/today").json()
    assert body["since"].endswith("+08:00")
    assert body["updated_at"].endswith("+08:00")


def test_stations_line_name_is_not_station_name(client, station_file):
    """线路名不能拿站名顶替。采集表只有站名时，lines[].name 必须是 null。"""
    station_file({"A01": {"line_id": "L1", "name": "人民广场"},
                  "A02": {"line_id": "L1", "name": "南京东路"}})
    lines = client.get("/api/stations").json()["lines"]
    assert lines[0]["line_id"] == "L1"
    assert lines[0]["name"] is None


def test_stations_line_name_uses_explicit_field(client, station_file):
    """采集表给了 line_name 才填线路名。"""
    station_file({"A01": {"line_id": "L1", "line_name": "一号线"}})
    assert client.get("/api/stations").json()["lines"][0]["name"] == "一号线"


def test_stations_does_not_leak_internal_line_name(client, station_file):
    """内部用的 line_name 不能出现在 stations[] 里。"""
    station_file({"A01": {"line_id": "L1", "name": "人民广场", "line_name": "一号线"}})
    station = client.get("/api/stations").json()["stations"][0]
    assert set(station) == {"station_id", "name", "line_id", "order",
                            "lng", "lat", "color"}


class CacheRedis:
    """带 setex 的最小 Redis 替身，用来验证今日汇总的缓存读写。"""

    def __init__(self):
        self.store = {}
        self.ttl = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value
        self.ttl[key] = ttl


@pytest.fixture
def cache_redis(monkeypatch):
    fake = CacheRedis()
    monkeypatch.setattr(views, "_client", fake)
    return fake


@pytest.mark.django_db
def test_summary_today_writes_cache(client, base_ts, cache_redis):
    """有数据时写缓存，键按日期分，TTL 取配置值。"""
    make_rows("A01", base_ts, count=1, passengers=5)
    key = views.summary_cache_key()
    client.get("/api/summary/today")
    assert key in cache_redis.store
    assert cache_redis.ttl[key] == settings.SUMMARY_CACHE_TTL


@pytest.mark.django_db
def test_summary_today_serves_cache(client, base_ts, cache_redis):
    """命中缓存时不再查库：把库清空，响应仍应给出缓存里的数字。"""
    make_rows("A01", base_ts, count=1, passengers=7)
    first = client.get("/api/summary/today").json()
    MetricWindow.objects.all().delete()
    second = client.get("/api/summary/today").json()
    assert second["total"] == first["total"] == 7


@pytest.mark.django_db
def test_summary_today_does_not_cache_degraded_result(client, monkeypatch, cache_redis):
    """查库降级时不能写缓存，否则一次数据库抖动会把 0 值钉在缓存里。"""
    break_db(monkeypatch)
    body = client.get("/api/summary/today").json()
    assert body["total"] == 0
    assert cache_redis.store == {}


@pytest.mark.django_db
def test_summary_today_ttl_zero_disables_cache(client, base_ts, cache_redis, monkeypatch):
    """TTL 配 0 表示不缓存，联调口径时能每次都看真实库。"""
    monkeypatch.setattr(settings, "SUMMARY_CACHE_TTL", 0)
    make_rows("A01", base_ts, count=1, passengers=5)
    client.get("/api/summary/today")
    assert cache_redis.store == {}


@pytest.mark.django_db
def test_summary_today_ignores_broken_cache(client, base_ts, cache_redis):
    """缓存内容坏了要回退查库，不能把坏数据吐给页面。"""
    make_rows("A01", base_ts, count=1, passengers=6)
    cache_redis.store[views.summary_cache_key()] = "{不是 JSON"
    assert client.get("/api/summary/today").json()["total"] == 6


# ---- 方向列尚未双写时：in/out 必须为 null，不能拿乘降量顶替 ----

def old_rows(station_id, base, count=2, passengers=5):
    """模拟计算端双写之前的写入：只填 passengers，in/out 两列保持默认 0。"""
    for i in range(count):
        MetricWindow.objects.create(
            ts=base + timedelta(seconds=i),
            station_id=station_id,
            passengers=passengers,
        )


@pytest.mark.django_db
def test_series_in_out_are_null_when_direction_unknown(client, base_ts):
    """关键：不能把合并乘降量报成进站量或出站量。"""
    old_rows("A01", base_ts, count=1, passengers=40)
    point = client.get("/api/series").json()["points"][0]
    assert point["in"] is None and point["out"] is None
    # 合并总量仍有值，旧前端读 passengers 不受影响
    assert point["total"] == 40 and point["passengers"] == 40


@pytest.mark.django_db
def test_series_direction_filter_does_not_invent_numbers(client, base_ts):
    """切到 out 时曾把总量报成出站量，这是编造，必须为 null。"""
    old_rows("A01", base_ts, count=1, passengers=40)
    point = client.get("/api/series?direction=out").json()["points"][0]
    assert point["in"] is None and point["out"] is None
    assert point["total"] == 40


@pytest.mark.django_db
def test_rank_in_out_are_null_when_direction_unknown(client, base_ts, station_file):
    """排行里名次仍按合并总量排，但分项给 null。"""
    station_file({"A01": {"line_id": "L1"}, "B01": {"line_id": "L1"}})
    old_rows("A01", base_ts, count=1, passengers=10)
    old_rows("B01", base_ts, count=1, passengers=30)
    top = client.get("/api/rank?top=2").json()["top"]
    assert [r["station_id"] for r in top] == ["B01", "A01"]
    assert all(r["in"] is None and r["out"] is None for r in top)
    assert [r["passengers"] for r in top] == [30, 10]


@pytest.mark.django_db
def test_rank_still_orders_by_total_when_direction_unknown(client, base_ts):
    """方向未知时切 out 不能把名次打乱，仍按已知的总量降序。"""
    old_rows("A01", base_ts, count=1, passengers=10)
    old_rows("B01", base_ts, count=1, passengers=30)
    top = client.get("/api/rank?direction=out&top=2").json()["top"]
    assert [r["passengers"] for r in top] == [30, 10]


@pytest.mark.django_db
def test_summary_today_null_direction_but_keeps_total(client, base_ts, station_file):
    station_file({"A01": {"line_id": "L1"}})
    old_rows("A01", base_ts, count=2, passengers=6)
    body = client.get("/api/summary/today").json()
    assert body["in"] is None and body["out"] is None
    assert body["net"] is None          # 缺一个方向就谈不上净流入
    assert body["total"] == 12          # 合并乘降量仍然可信
    assert body["lines"][0]["total"] == 12
    assert body["lines"][0]["in"] is None
    assert body["top_stations"][0]["total"] == 12


@pytest.mark.django_db
def test_summary_today_known_direction_gives_numbers(client, base_ts):
    """一旦计算端双写，in/out/net 立刻变成真实数字，不需要改代码。"""
    make_rows("A01", base_ts, count=1, passengers=10, in_p=8, out_p=2)
    body = client.get("/api/summary/today").json()
    assert (body["in"], body["out"], body["net"], body["total"]) == (8, 2, 6, 10)
