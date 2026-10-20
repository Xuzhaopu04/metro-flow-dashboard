import json
from datetime import timedelta

import redis
from django.conf import settings
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

from .models import MetricWindow

EMPTY_SNAPSHOT = {"ts": None, "total": 0, "stations": [], "late_count": 0, "invalid_count": 0}

# 契约没限定排行的统计时长，这里定死 5 分钟，并在指标口径文档里写明
RANK_MINUTES = 5
MAX_MINUTES = 60
MAX_TOP = 20

_client = None


def get_redis():
    """延迟建连接：服务刚起来时 Redis 可能还没好，不能在导入阶段就连。"""
    global _client
    if _client is None:
        _client = redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            decode_responses=True,
            socket_connect_timeout=2,
        )
    return _client


def _json(payload, status=200):
    return JsonResponse(payload, status=status, json_dumps_params={"ensure_ascii": False})


def _error(code, message, status):
    return _json({"error": {"code": code, "message": message}}, status)


def dashboard(request):
    """大屏页面。和接口同源渲染，省掉跨域那一堆配置。"""
    return render(request, "index.html")


def snapshot(request):
    """最新一秒的指标快照，契约 5.1。没有数据也返回 200。"""
    response = _json(_read_snapshot())
    # 页面每秒拉一次，被浏览器或代理缓存住就会看起来"卡住不动"
    response["Cache-Control"] = "no-store"
    return response


def _read_snapshot():
    try:
        raw = get_redis().get(settings.SNAPSHOT_KEY)
    except redis.RedisError:
        return EMPTY_SNAPSHOT
    if not raw:
        return EMPTY_SNAPSHOT
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 快照写坏了也要让页面能开，宁可显示空
        return EMPTY_SNAPSHOT


def series(request):
    """历史序列，契约 5.2。不传 station_id 就按时间戳汇总全部站点。"""
    raw_minutes = request.GET.get("minutes", "5")
    try:
        minutes = int(raw_minutes)
    except ValueError:
        return _error("INVALID_PARAM", f"minutes 必须是整数，收到 {raw_minutes}", 400)
    if not 1 <= minutes <= MAX_MINUTES:
        return _error("INVALID_PARAM", f"minutes 必须在 1—{MAX_MINUTES} 之间", 400)

    station_id = request.GET.get("station_id") or None
    since = timezone.now() - timedelta(minutes=minutes)

    queryset = MetricWindow.objects.filter(ts__gte=since)
    if station_id:
        queryset = queryset.filter(station_id=station_id)

    rows = queryset.values("ts").annotate(passengers=Sum("passengers")).order_by("ts")
    points = [
        {"ts": row["ts"].isoformat(timespec="seconds"), "passengers": row["passengers"] or 0}
        for row in rows
    ]
    return _json({"station_id": station_id, "points": points})


def rank(request):
    """站点客流排行，契约 5.3。统计窗口固定为最近 5 分钟。"""
    raw_top = request.GET.get("top", "5")
    try:
        top = int(raw_top)
    except ValueError:
        return _error("INVALID_PARAM", f"top 必须是整数，收到 {raw_top}", 400)
    if not 1 <= top <= MAX_TOP:
        return _error("INVALID_PARAM", f"top 必须在 1—{MAX_TOP} 之间", 400)

    since = timezone.now() - timedelta(minutes=RANK_MINUTES)
    rows = (
        MetricWindow.objects.filter(ts__gte=since)
        .values("station_id")
        .annotate(passengers=Sum("passengers"))
        .order_by("-passengers")[:top]
    )
    top_rows = [{"station_id": r["station_id"], "passengers": r["passengers"]} for r in rows]
    return _json({"top": top_rows})


def not_found(request, exception=None):
    return _error("NOT_FOUND", "接口不存在", 404)


def server_error(request):
    return _error("INTERNAL_ERROR", "服务端异常", 500)
