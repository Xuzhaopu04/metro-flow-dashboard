"""接口层测试：参数校验、空数据降级、404 结构。

用 config.settings_test 里的 SQLite 与失效的 Redis 端口，不需要外部依赖。
"""

import json
from datetime import timedelta

import pytest
from django.db import DatabaseError
from django.utils import timezone

from api import views
from api.models import MetricWindow


class FakeRedis:
    def __init__(self, payload=None):
        self.payload = payload

    def get(self, key):
        return self.payload


def make_rows(station_id, base, count=3, passengers=5):
    """造 count 行，时间从 base 起每秒一行。base 由调用方给定，避免各站时间戳错位。"""
    for i in range(count):
        MetricWindow.objects.create(
            ts=base + timedelta(seconds=i),
            station_id=station_id,
            passengers=passengers,
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
    assert client.get("/api/snapshot").json() == payload


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
    # 契约 4.2 只认 station_id 与 points，不能自创字段
    assert set(body) == {"station_id", "points"}


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
