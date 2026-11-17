"""接口层：快照、序列、排行，以及大屏页面。

契约见 docs/02-设计/接口契约.md 第 4 节，指标口径见 docs/02-设计/指标口径.md。
这一层只做三件事：取参数、查数据、按契约拼 JSON；聚合逻辑不在这里。

降级原则：Redis 或 MySQL 出问题时返回空结构而不是 5xx。页面要能打开，
显示"暂无数据"，而不是把运维问题甩成前端报错。
"""

import json
import logging
import re
from datetime import timedelta

import redis
from django.conf import settings
from django.db import DatabaseError
from django.db.models import Count, Sum
from django.db.models.functions import TruncHour, TruncMinute
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import MetricWindow
from .stations import build_line_map, stations_of_line, stations_payload

# 降级是有代价的（数据缺失），每次都要在日志里留下痕迹，否则排障时无从查起
logger = logging.getLogger(__name__)

# 契约 4.2 / 4.3 规定的参数范围，数字必须与文档一致，改这里就要同步改契约
DEFAULT_MINUTES = 5
MAX_MINUTES = 60
DEFAULT_TOP = 5
MAX_TOP = 20
TOP_STATIONS = 5       # 契约 4.4：今日汇总里分站 TopN 固定 5 条

# v1.3 新增：方向枚举与聚合粒度
DIRECTIONS = ("in", "out", "all")
GRANULARITIES = ("second", "minute", "hour")
DEFAULT_GRANULARITY = "second"
# 契约 4.2：秒级点数上限，超出抽稀并标记 truncated
MAX_POINTS = 2000
# 契约 4.2：from/to 跨度上限，防止一次查询扫全表
MAX_SPAN_HOURS = 24

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


def alert_state(total, threshold):
    """按契约 4.1 计算告警字段。

    判定放在服务端（责任矩阵接口点 ④"谁写=服务"）：阈值改一次配置就全站生效，
    不用动消费者，也不用重新发前端。

    用 >= 而不是 >：恰好等于阈值也算告警。前端原先写的就是 >=，
    这里保持一致；把边界值判成"安全"在演示时最容易说不清。
    """
    return {"alerting": total >= threshold, "threshold": threshold}


def empty_snapshot():
    """空快照。每次返回新对象——共享可变默认值迟早被某次改动写脏。

    无数据时同样要带告警字段：前端只认一个响应形状，不该在这里分支。
    阈值若被配成 0 则 0 >= 0 也会告警，这是配置问题，如实反映即可。
    """
    snapshot = {"ts": None, "total": 0, "stations": [], "late_count": 0, "invalid_count": 0}
    snapshot.update(alert_state(0, settings.ALERT_THRESHOLD))
    return snapshot


def normalize_snapshot(payload):
    """按契约 3 补齐快照字段，不信任上游。

    快照是跨进程传过来的 JSON，缺一个键前端就读到 undefined 并画出空白图表。
    上游写坏了是上游的问题，但页面白屏会被记成接口的锅，所以这里兜住。

    告警字段不取上游的值，一律按当前配置重算。消费者写快照时并不知道阈值，
    若采信它写的 alerting，阈值一改页面就挂着过期的判断。
    """
    total = payload.get("total") or 0
    snapshot = {
        "ts": payload.get("ts"),
        "total": total,
        "stations": payload.get("stations") or [],
        "late_count": payload.get("late_count") or 0,
        "invalid_count": payload.get("invalid_count") or 0,
    }
    snapshot.update(alert_state(total, settings.ALERT_THRESHOLD))
    return snapshot


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


def choice_param(request, name, choices, default):
    """取枚举型查询参数，返回 (值, 错误响应)。

    不传或空串走默认值；传了非法值就报错而不是悄悄回退——
    回退会让前端以为筛选生效了，实际看的是全量数据，这种"静默错误"最难查。
    """
    raw = request.GET.get(name)
    if raw is None or raw.strip() == "":
        return default, None
    value = raw.strip().lower()
    if value not in choices:
        allowed = " / ".join(choices)
        return None, error_response(
            "INVALID_PARAM", f"{name} 只能是 {allowed}，收到 {raw}", 400
        )
    return value, None


def time_param(request, name):
    """取 ISO8601 时间参数，返回 (datetime, 错误响应)。

    Django 的 parse_datetime 要求带日期部分；只给时间会返回 None，
    这里一并当成格式错误，避免"看起来传了参数其实没生效"。

    时区里的 `+` 必须兜住：查询串里未编码的 `+08:00` 会被解析成空格 ` 08:00`，
    parse_datetime 随即失败。这是前端最容易踩的坑——直接拼 `isoformat()` 就中招。
    把它们折回 `+` 比让调用方永远记得 urlencode 更可靠，也让 400 只在真的写错时出现。
    """
    raw = request.GET.get(name)
    if raw is None or raw.strip() == "":
        return None, None
    text = raw.strip()
    # 形如 "2026-11-17T14:55:01 08:00" 且末尾是 6 位时区偏移，还原成 +08:00
    if re.search(r"\s\d{2}:\d{2}$", text):
        text = re.sub(r"\s(\d{2}:\d{2})$", r"+\1", text)
    value = parse_datetime(text)
    if value is None:
        return None, error_response(
            "INVALID_PARAM", f"{name} 必须是 ISO8601 时间，收到 {raw}", 400
        )
    # 不带时区的时间按项目时区解释，否则 MySQL 里存的是 UTC 会对不上 8 小时
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return value, None


def resolve_window(request):
    """决定查询的时间区间，返回 (since, until, error)。

    from/to 同时给出就用它们，否则退回 minutes。契约 4.2 这么规定，
    是为了让前端做"查某天某个时段"时不用把跨度换算成分钟数。
    """
    start, error = time_param(request, "from")
    if error:
        return None, None, error
    end, error = time_param(request, "to")
    if error:
        return None, None, error

    now = timezone.now()
    if start or end:
        until = end or now
        since = start or (until - timedelta(minutes=DEFAULT_MINUTES))
        if since > until:
            return None, None, error_response(
                "INVALID_PARAM", "from 不能晚于 to", 400
            )
        if until - since > timedelta(hours=MAX_SPAN_HOURS):
            return None, None, error_response(
                "INVALID_PARAM", f"时间跨度不能超过 {MAX_SPAN_HOURS} 小时", 400
            )
        return since, until, None

    minutes, error = int_param(request, "minutes", DEFAULT_MINUTES, 1, MAX_MINUTES)
    if error:
        return None, None, error
    return now - timedelta(minutes=minutes), now, None


def direction_sums(in_sum, out_sum, total, direction):
    """把 (进站, 出站, 合并总量) 归成对外展示的 (in, out, total)。

    关键约定：**方向未知时 in/out 返回 None，不拿总量顶替**。
    计算端双写 in/out 之前，两列恒为 0，而 passengers 是进+出的合并乘降量。
    早先的写法把总量记到所选方向上，于是 `?direction=out` 会报
    "出站 1188"——那 1188 其实是进出之和，属于编造。人数字段宁可给 null，
    也不能给一个看起来合理、实际口径错误的数，否则前端与报表都会照着错。

    判定"已知"的依据：两列不全为 0。两列都为 0 但总量大于 0，只能是老数据；
    总量也为 0 时是真的没数据，返回 0 更自然。
    """
    if in_sum == 0 and out_sum == 0:
        return (0, 0, 0) if not total else (None, None, total)
    if direction == "in":
        return in_sum, 0, in_sum
    if direction == "out":
        return 0, out_sum, out_sum
    return in_sum, out_sum, in_sum + out_sum


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


def collect_points(rows, direction="all"):
    """把聚合结果转成契约 4.2 的 points 结构。

    每点同时给 `in`/`out`/`total`，并保留 v1.2 的 `passengers`（等于 total）：
    契约承诺"旧调用不传参不受影响"，旧前端读 `passengers` 必须还能画出线。
    """
    points = []
    for row in rows:
        in_sum, out_sum, point_total = direction_sums(
            row.get("in_p") or 0,
            row.get("out_p") or 0,
            row.get("passengers") or 0,
            direction,
        )
        points.append({
            # 秒级粒度不分桶，行的键是 ts；分桶后键是 bucket
            "ts": local_iso(row.get("bucket") or row["ts"]),
            "in": in_sum,
            "out": out_sum,
            "total": point_total,
            # passengers 是 v1.2 的合并总量，方向未知时它仍有值，供旧前端继续画线
            "passengers": point_total,
        })
    return points


def thin_points(points, limit=MAX_POINTS):
    """点数超上限时等间隔抽稀，保留首尾点。返回 (点数, 是否抽稀)。

    抽稀而不是截断：截断会让曲线只剩前半段，看起来像"后半段没数据"；
    等间隔抽稀保持整体形状，首尾一定保留，趋势判断不失真。
    """
    if len(points) <= limit:
        return points, False
    step = (len(points) - 1) / (limit - 1)
    picked = [points[round(i * step)] for i in range(limit)]
    return picked, True


def trunc_fn(granularity):
    """按粒度选聚合函数。秒级不用 Trunc——表里本来就是每秒一行。"""
    if granularity == "minute":
        return TruncMinute
    if granularity == "hour":
        return TruncHour
    return None


def fetch_series(since, until, station_id, line_id, granularity):
    """按时间区间查序列并按粒度聚合。

    先按 ts 分桶求和，再把 in/out 一起带出来：方向筛选放在 Python 里做，
    避免为三种 direction 拼三条 SQL——口径只有一处，不会各写一遍。
    """
    rows = MetricWindow.objects.filter(ts__gte=since, ts__lte=until)
    if station_id:
        rows = rows.filter(station_id=station_id)
    if line_id:
        members = stations_of_line(line_id)
        if not members:
            return []
        rows = rows.filter(station_id__in=members)

    fn = trunc_fn(granularity)
    key = "bucket" if fn else "ts"
    if fn:
        rows = rows.annotate(bucket=fn("ts", tzinfo=timezone.get_current_timezone()))
    return list(
        rows.values(key)
        .annotate(
            passengers=Sum("passengers"),
            in_p=Sum("in_passengers"),
            out_p=Sum("out_passengers"),
        )
        .order_by(key)
    )


def series(request):
    """历史序列，契约 4.2。v1.3 扩参，不传新参数时行为与 v1.2 一致。"""
    since, until, error = resolve_window(request)
    if error:
        return error

    direction, error = choice_param(request, "direction", DIRECTIONS, "all")
    if error:
        return error
    granularity, error = choice_param(
        request, "granularity", GRANULARITIES, DEFAULT_GRANULARITY
    )
    if error:
        return error

    station_id = request.GET.get("station_id") or None
    line_id = request.GET.get("line_id") or None

    def fetch():
        rows = fetch_series(since, until, station_id, line_id, granularity)
        return thin_points(collect_points(rows, direction))

    points, truncated = query_rows(fetch, ([], False))
    return json_response({
        "station_id": station_id,
        "line_id": line_id,
        "direction": direction,
        "granularity": granularity,
        "truncated": truncated,
        "points": points,
    })


def fetch_rank(since, until, line_id, direction, top):
    """按站聚合最近一段时间的客流，按所选方向降序取前 N。"""
    rows = MetricWindow.objects.filter(ts__gte=since, ts__lte=until)
    if line_id:
        members = stations_of_line(line_id)
        if not members:
            return []
        rows = rows.filter(station_id__in=members)

    rows = rows.values("station_id").annotate(
        passengers=Sum("passengers"),
        in_p=Sum("in_passengers"),
        out_p=Sum("out_passengers"),
    )
    # 排序必须跟着"实际展示的那个数"走，不能直接 order_by("-in_p")：
    # in/out 两列在计算端双写之前全是 0，SQL 按 0 排序结果不确定，
    # 会出现榜单第一名 300、第三名 400 这种和数字自相矛盾的顺序。
    # 站点数很少，取全量后在 Python 里按算出来的值排，口径只有一处。
    scored = []
    for row in rows:
        in_sum, out_sum, row_total = direction_sums(
            row["in_p"] or 0, row["out_p"] or 0, row["passengers"] or 0, direction
        )
        # 方向未知时 in/out 是 None，退而按已知的合并总量排名次：
        # 名次仍然正确，只是该行不会谎报"进站多少"。
        key = {"in": in_sum, "out": out_sum}.get(direction)
        scored.append((key if key is not None else row_total, row["station_id"],
                       in_sum, out_sum, row_total))
    # 客流相同时按站点编码升序，保证同一份数据每次请求名次一致
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [
        {"station_id": sid, "in": in_sum, "out": out_sum, "passengers": row_total}
        for _, sid, in_sum, out_sum, row_total in scored[:top]
    ]


def rank(request):
    """站点客流排行，契约 4.3。默认仍为最近 5 分钟。"""
    top, error = int_param(request, "top", DEFAULT_TOP, 1, MAX_TOP)
    if error:
        return error
    minutes, error = int_param(request, "minutes", DEFAULT_MINUTES, 1, MAX_MINUTES)
    if error:
        return error
    direction, error = choice_param(request, "direction", DIRECTIONS, "all")
    if error:
        return error

    line_id = request.GET.get("line_id") or None
    now = timezone.now()
    since = now - timedelta(minutes=minutes)

    rows = query_rows(lambda: fetch_rank(since, now, line_id, direction, top), [])
    return json_response({"top": rows})


def today_start():
    """今日统计起点：项目时区当天 0 点。契约 4.4 要求必须注明起点。

    不是"最近 24 小时"：演示时"今日累计"要能对上报表，
    跨天滚动的时间窗在 0 点附近会给出一个和昨天混在一起的数。
    """
    return timezone.localtime(timezone.now()).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def fetch_today(since):
    """一次分组查询拿到今日全部聚合，避免为每个指标各扫一遍表。

    按站分组后，总量、分线、TopN 都能在 Python 里加出来；
    多查几次的代价是同一个区间被扫多遍，数据量大时明显。
    """
    return list(
        MetricWindow.objects.filter(ts__gte=since)
        .values("station_id")
        .annotate(
            passengers=Sum("passengers"),
            in_p=Sum("in_passengers"),
            out_p=Sum("out_passengers"),
            late=Sum("late_count"),
            rows=Count("id"),
        )
    )


def build_today(rows, since, now):
    """把分组结果拼成契约 4.4 的响应体。

    方向未知时（计算端尚未双写 in/out）`in`/`out`/`net` 为 `null`，
    `total` 仍给合并乘降量：宁可让前端显示"进站 —"，也不能把乘降量
    当成进站量报出去。
    """
    in_sum, out_sum, total = direction_sums(
        sum(r["in_p"] or 0 for r in rows),
        sum(r["out_p"] or 0 for r in rows),
        sum(r["passengers"] or 0 for r in rows),
        "all",
    )
    return {
        "date": since.strftime("%Y-%m-%d"),
        "since": local_iso(since),
        # updated_at 也要本地时区：和 since、points[].ts 混着用两种时区，
        # 前端算"数据延迟几秒"会差 8 小时
        "updated_at": local_iso(now),
        "total": total,
        "in": in_sum,
        "out": out_sum,
        # 任一方向未知就无法算净流入，此时给 null 而不是 0
        "net": None if in_sum is None or out_sum is None else in_sum - out_sum,
        "peak": today_peak(since),
        "lines": today_lines(rows),
        "top_stations": today_top(rows),
        "quality": {
            "late_count": sum(r["late"] or 0 for r in rows),
            "rows": sum(r["rows"] or 0 for r in rows),
        },
    }


def today_peak(since):
    """今日单窗口最大乘降量及发生时刻。窗口是 1 秒，先按 ts 汇总再取最大。

    走 query_rows 而不是直接查：数据库挂掉时这里若抛 DatabaseError，
    整个 /api/summary/today 会变 500，而契约要求无数据也给 0 值结构。
    """

    def fetch():
        row = (
            MetricWindow.objects.filter(ts__gte=since)
            .values("ts")
            .annotate(total=Sum("passengers"))
            .order_by("-total")
            .first()
        )
        if not row:
            return {"ts": None, "total": 0}
        return {"ts": local_iso(row["ts"]), "total": row["total"] or 0}

    return query_rows(fetch, {"ts": None, "total": 0})


def today_lines(rows):
    """按线路汇总。metric_window 没有 line_id 列，映射关系只存在站点表里。

    与总量同样的口径：方向未知时线路的 `in`/`out` 是 `null`，
    `total` 仍给合并值。逐站累加时跳过 None，最后按是否出现过未知来决定。
    """
    mapping = build_line_map()
    grouped = {}
    for row in rows:
        line_id = mapping.get(row["station_id"])
        bucket = grouped.setdefault(
            line_id,
            {"line_id": line_id, "in": 0, "out": 0, "total": 0, "unknown": False},
        )
        in_sum, out_sum, row_total = direction_sums(
            row["in_p"] or 0, row["out_p"] or 0, row["passengers"] or 0, "all"
        )
        bucket["total"] += row_total
        if in_sum is None:
            bucket["unknown"] = True
        else:
            bucket["in"] += in_sum
            bucket["out"] += out_sum
    result = []
    for bucket in grouped.values():
        if bucket["unknown"]:
            bucket["in"] = bucket["out"] = None
        bucket.pop("unknown")
        result.append(bucket)
    return sorted(result, key=lambda b: (-b["total"], b["line_id"] or ""))


def today_top(rows, limit=TOP_STATIONS):
    """今日分站 TopN。方向未知时 in/out 为 null，排序仍按合并总量。"""
    result = []
    for row in rows:
        in_sum, out_sum, row_total = direction_sums(
            row["in_p"] or 0, row["out_p"] or 0, row["passengers"] or 0, "all"
        )
        result.append({
            "station_id": row["station_id"],
            "in": in_sum,
            "out": out_sum,
            "total": row_total,
        })
    result.sort(key=lambda item: (-item["total"], item["station_id"]))
    return result[:limit]


def summary_cache_key():
    """按日期分键：跨零点后旧键自然不再被读到，不用额外清理。"""
    return f"metric:summary:{today_start().strftime('%Y%m%d')}"


def read_summary_cache():
    """读今日汇总缓存。没有、过期、JSON 坏、Redis 挂，一律返回 None 视为未命中。"""
    if settings.SUMMARY_CACHE_TTL <= 0:
        return None
    try:
        raw = get_redis().get(summary_cache_key())
    except redis.RedisError:
        # 缓存只是省一次查询，读不到就查库；绝不因为缓存不可用而让接口失败
        logger.warning("读今日汇总缓存失败，回退查库", exc_info=True)
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("今日汇总缓存内容损坏，回退查库")
        return None
    return payload if isinstance(payload, dict) else None


def write_summary_cache(payload):
    """写缓存，失败只记日志。缓存写不进去不该影响这次已经算好的响应。"""
    if settings.SUMMARY_CACHE_TTL <= 0:
        return
    try:
        get_redis().setex(
            summary_cache_key(), settings.SUMMARY_CACHE_TTL,
            json.dumps(payload, ensure_ascii=False),
        )
    except redis.RedisError:
        logger.warning("写今日汇总缓存失败，本次结果照常返回", exc_info=True)


def summary_today(request):
    """今日汇总，契约 4.4。无数据时返回 0 值结构而不是 404。

    带缓存是因为要扫当日全表。命中缓存时 `updated_at` 仍是上次计算的时刻——
    它表示"这份数字算于何时"，不是"何时返回给你"，前端据此算新鲜度才不会误判。
    """
    cached = read_summary_cache()
    if cached:
        return json_response(cached)

    since = today_start()
    now = timezone.now()
    rows = query_rows(lambda: fetch_today(since), [])
    payload = build_today(rows, since, now)
    # 查库降级（空结果）时不写缓存，否则一次数据库抖动会把 0 值钉在缓存里
    if rows:
        write_summary_cache(payload)
    return json_response(payload)


def stations(request):
    """站点元数据，契约 4.5。直接读采集表的实时内容，不做复制与缓存。"""
    return json_response(stations_payload())


def not_found(request, exception=None):
    return error_response("NOT_FOUND", "接口不存在", 404)


def server_error(request):
    return error_response("INTERNAL_ERROR", "服务端异常", 500)
