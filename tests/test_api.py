"""接口层测试：参数校验、空数据降级、404 结构。

用 config.settings_test 里的 SQLite 与失效的 Redis 端口，不需要外部依赖。
"""

import json
import logging
from datetime import timedelta

import pytest
import redis
from django.conf import settings
from django.utils import timezone

from api import views
from api.models import MetricWindow
from config.settings import parse_positive_int


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


# ---- 告警接口（契约 4.4）----


def test_alert_falls_back_when_snapshot_missing(client):
    """没有快照时接口仍要 200，并且不报"告警"，否则页面一开就红。"""
    body = client.get("/api/alert").json()
    assert body["current"] == 0
    assert body["alerting"] is False
    assert body["threshold"] == settings.ALERT_THRESHOLD


def test_alert_reports_current_value(client, fake_redis):
    payload = {"ts": "2026-10-20T09:30:15+08:00", "total": 37,
               "stations": [], "late_count": 0, "invalid_count": 0}
    fake_redis(json.dumps(payload))
    body = client.get("/api/alert").json()
    assert body["current"] == 37
    assert body["alerting"] is False


@pytest.mark.parametrize("total, expected", [(99, False), (100, True), (101, True)])
def test_alert_threshold_boundary(client, fake_redis, total, expected):
    """边界定在"≥阈值即告警"，与用例表 UC-17 的文案一致。"""
    payload = {"ts": "2026-10-20T09:30:15+08:00", "total": total,
               "stations": [], "late_count": 0, "invalid_count": 0}
    fake_redis(json.dumps(payload))
    assert client.get("/api/alert").json()["alerting"] is expected


def test_alert_uses_configured_threshold(client, fake_redis, settings):
    """阈值来自配置而不是写死：改 settings 就该生效，这是本次改造的验收点。"""
    settings.ALERT_THRESHOLD = 50
    payload = {"ts": "2026-10-20T09:30:15+08:00", "total": 60,
               "stations": [], "late_count": 0, "invalid_count": 0}
    fake_redis(json.dumps(payload))
    body = client.get("/api/alert").json()
    assert body["threshold"] == 50
    assert body["alerting"] is True


def test_alert_falls_back_on_broken_json(client, fake_redis):
    """快照写坏了按空快照处理，不能让告警接口 500。"""
    fake_redis("{坏的快照")
    body = client.get("/api/alert").json()
    assert body["current"] == 0
    assert body["alerting"] is False


def test_alert_falls_back_when_redis_down(client, monkeypatch):
    """Redis 连不上时告警接口要降级，不能把异常抛给页面。"""

    class BrokenRedis:
        def get(self, key):
            raise redis.RedisError("connection refused")

    monkeypatch.setattr(views, "_client", BrokenRedis())
    response = client.get("/api/alert")
    assert response.status_code == 200
    body = response.json()
    assert body["current"] == 0
    assert body["alerting"] is False


def test_alert_is_not_cached(client):
    """告警状态每秒都变，被缓存住页面就会一直挂着旧横幅。"""
    assert client.get("/api/alert")["Cache-Control"] == "no-store"


# ---- 阈值配置解析（config.settings.parse_positive_int）----


@pytest.mark.parametrize("raw, expected", [
    ("50", 50),
    ("  120  ", 120),   # .env 里手抖多打了空格
    (None, 100),        # 没配
    ("", 100),          # 配了但留空，属于"没配"
    ("   ", 100),
    ("abc", 100),       # 写错不能把服务拖挂
    ("3.7", 100),
    ("0", 100),         # 0 会让告警永远成立，无意义
    ("-5", 100),
])
def test_parse_positive_int(raw, expected):
    assert parse_positive_int(raw, 100, "ALERT_THRESHOLD") == expected


def test_parse_positive_int_logs_on_bad_value(caplog):
    """配错了要留下痕迹，否则线上排查时只会觉得"阈值怎么不生效"。"""
    with caplog.at_level(logging.WARNING):
        parse_positive_int("abc", 100, "ALERT_THRESHOLD")
    assert "ALERT_THRESHOLD" in caplog.text


def test_parse_positive_int_silent_when_unset(caplog):
    """只是没配而已，不该刷日志。"""
    with caplog.at_level(logging.WARNING):
        parse_positive_int(None, 100, "ALERT_THRESHOLD")
    assert caplog.text == ""
