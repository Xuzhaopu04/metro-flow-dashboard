"""接口层：快照、序列、排行，以及大屏页面。

契约见 docs/02-设计/接口契约.md 第 4 节，指标口径见 docs/02-设计/指标口径.md。
这一层只做三件事：取参数、查数据、按契约拼 JSON；聚合逻辑不在这里。

降级原则：Redis 或 MySQL 出问题时返回空结构而不是 5xx。页面要能打开，
显示"暂无数据"，而不是把运维问题甩成前端报错。
"""

import json
import logging
from datetime import timedelta

import redis
from django.conf import settings
from django.db import DatabaseError
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

from .models import MetricWindow

# 降级是有代价的（数据缺失），每次都要在日志里留下痕迹，否则排障时无从查起
logger = logging.getLogger(__name__)

# 契约 4.2 / 4.3 规定的参数范围，数字必须与文档一致，改这里就要同步改契约
DEFAULT_MINUTES = 5
MAX_MINUTES = 60
RANK_MINUTES = 5      # 契约 4.3：排行固定统计最近 5 分钟，不接受入参
DEFAULT_TOP = 5
MAX_TOP = 20

API_PREFIX = "/api/"

# 模块级单例。测试通过 monkeypatch 覆盖它来注入假客户端，
# 所以名字沿用 _client，改成别的会让 tests/test_api.py 的 fixture 失效
_client = None


def get_redis():
    """延迟建连：进程刚起来时 Redis 可能还没好，导入阶段不能连。"""
    global _client
    if _client is None:
        _client = redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            decode_responses=True,
            socket_connect_timeout=2,
        )
    return _client


def json_response(payload, status=200):
    """统一的 JSON 出口：中文不转义，日志和页面里都能直接看。

    Cache-Control 在这里统一设，不靠每个 view 自己记得加：
    本模块所有响应都是实时数据或错误，没有一个该被缓存。
    漏在某一处，那个接口就可能被浏览器或代理缓存住，页面看起来"卡住不动"。
    """
    response = JsonResponse(
        payload, status=status, json_dumps_params={"ensure_ascii": False}
    )
    response["Cache-Control"] = "no-store"
    return response


def error_response(code, message, status):
    """契约第 1 节规定的错误体结构，全站只有这一处拼装。"""
    return json_response({"error": {"code": code, "message": message}}, status)


def empty_snapshot():
    """空快照。每次返回新对象——共享可变默认值迟早被某次改动写脏。"""
    return {"ts": None, "total": 0, "stations": [], "late_count": 0, "invalid_count": 0}


def normalize_snapshot(payload):
    """按契约 3 补齐快照字段，不信任上游。

    快照是跨进程传过来的 JSON，缺一个键前端就读到 undefined 并画出空白图表。
    上游写坏了是上游的问题，但页面白屏会被记成接口的锅，所以这里兜住。
    """
    return {
        "ts": payload.get("ts"),
        "total": payload.get("total") or 0,
        "stations": payload.get("stations") or [],
        "late_count": payload.get("late_count") or 0,
        "invalid_count": payload.get("invalid_count") or 0,
    }


def read_snapshot():
    """读最新快照。Redis 不可用、键过期、JSON 损坏，一律退化成空快照。"""
    try:
        raw = get_redis().get(settings.SNAPSHOT_KEY)
    except redis.RedisError:
        return empty_snapshot()
    if not raw:
        return empty_snapshot()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return empty_snapshot()
    # 合法 JSON 不等于合法快照：可能是数组、数字或字符串，后面 .get 会炸
    if not isinstance(payload, dict):
        return empty_snapshot()
    return normalize_snapshot(payload)


def local_iso(value):
    """把库里的时间转成契约要求的带时区 ISO8601 字符串。

    MySQL 存的是 UTC（USE_TZ=True），直接 isoformat 出来是 +00:00；
    而快照里的 ts 由消费者按本机时区写成 +08:00。同一个页面同时显示这两处，
    差 8 小时会被当成 bug，所以这里统一换算到项目时区。
    """
    return timezone.localtime(value).isoformat(timespec="seconds")


def int_param(request, name, default, low, high):
    """取整数查询参数并校验范围，返回 (值, 错误响应)。

    错误响应为 None 表示参数合法。series 与 rank 的规则完全一样，
    抽出来是为了两处的边界数字和报错文案不会各写一套、日后各改一遍。
    参数缺省或传空串都按默认值走——契约里"不传"和"传空"都该是默认行为。
    """
    raw = request.GET.get(name)
    if raw is None or raw.strip() == "":
        return default, None
    try:
        value = int(raw)
    except ValueError:
        return None, error_response("INVALID_PARAM", f"{name} 必须是整数，收到 {raw}", 400)
    if not low <= value <= high:
        return None, error_response("INVALID_PARAM", f"{name} 必须在 {low}—{high} 之间", 400)
    return value, None


def query_rows(fn, fallback):
    """执行数据库查询，出问题时降级到 fallback，返回查询结果。

    MySQL 挂了只该让历史与排行查不到，实时快照仍可用（架构说明第 3 节）。
    不接住 DatabaseError 的话，页面拿到的是 500 甚至 HTML 报错页，
    把"数据库在维护"变成"系统坏了"，这不是同一件事。

    降级后返回空数组，页面按契约既定行为显示"暂无数据"——
    不额外加 degraded 之类的字段，契约没定义的字段一个都不能加。

    DatabaseError 覆盖 Django 包装后的连接失败、超时、SQL 错误；
    不用裸 except，是因为编程错误（如拼错字段）应当暴露出来而不是被吞掉。
    """
    try:
        return fn()
    except DatabaseError:
        logger.warning("查询数据库失败，降级为空结果", exc_info=True)
        return fallback


def dashboard(request):
    """大屏页面。和接口同源渲染，省掉跨域配置，也省掉前端构建链。"""
    return render(request, "index.html")


def snapshot(request):
    """最新一秒的指标快照，契约 4.1。没有数据也返回 200。"""
    return json_response(read_snapshot())


def collect_points(rows):
    """把聚合结果转成契约 4.2 的 points 结构。"""
    return [
        {"ts": local_iso(row["ts"]), "passengers": row["passengers"] or 0}
        for row in rows
    ]


def series(request):
    """历史序列，契约 4.2。不传 station_id 就按时间戳汇总全部站点。"""
    minutes, error = int_param(request, "minutes", DEFAULT_MINUTES, 1, MAX_MINUTES)
    if error:
        return error

    station_id = request.GET.get("station_id") or None
    since = timezone.now() - timedelta(minutes=minutes)

    def fetch():
        rows = MetricWindow.objects.filter(ts__gte=since)
        if station_id:
            rows = rows.filter(station_id=station_id)
        rows = rows.values("ts").annotate(passengers=Sum("passengers")).order_by("ts")
        return collect_points(rows)

    points = query_rows(fetch, [])
    return json_response({"station_id": station_id, "points": points})


def rank(request):
    """站点客流排行，契约 4.3。统计窗口固定为最近 5 分钟。"""
    top, error = int_param(request, "top", DEFAULT_TOP, 1, MAX_TOP)
    if error:
        return error

    since = timezone.now() - timedelta(minutes=RANK_MINUTES)

    def fetch():
        rows = (
            MetricWindow.objects.filter(ts__gte=since)
            .values("station_id")
            .annotate(passengers=Sum("passengers"))
            .order_by("-passengers")[:top]
        )
        # 契约要求人数是整数：Sum 在空集上是 None，这里一并兜成 0
        return [
            {"station_id": row["station_id"], "passengers": row["passengers"] or 0}
            for row in rows
        ]

    return json_response({"top": query_rows(fetch, [])})


def not_found(request, exception=None):
    return error_response("NOT_FOUND", "接口不存在", 404)


def server_error(request):
    return error_response("INTERNAL_ERROR", "服务端异常", 500)
