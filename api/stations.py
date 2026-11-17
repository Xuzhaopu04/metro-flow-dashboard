"""站点元数据：读采集端的 stations.json，不在本层复制数据。

契约 4.5 要求"只读采集的 stations.json"，所以这里每次都重新读文件而不是缓存。
理由：站点表是人工维护的小文件，改动频率低但演示前可能临时调；一旦缓存住，
改完要重启才生效，联调时最难判断"到底是没改还是没生效"。

容错姿态是"缺就返回 null，不编造"：采集端这份表目前只有 line_id 和 weight，
名称、顺序、坐标都还没有。等采集补齐就自动出现，服务端不用跟着改。
"""

import json
import logging
import os

from django.conf import settings

logger = logging.getLogger(__name__)

# 与 simulator/simulator.py 读的是同一个文件，路径由 settings 统一定义
def stations_file():
    """站点表路径。放 settings 里是为了测试能指向临时文件。"""
    return os.path.join(settings.BASE_DIR, "simulator", "stations.json")


def load_stations():
    """读站点表，返回 {station_id: {...}}。读不到、格式坏，一律返回空字典。

    这层不抛异常：站点表只是辅助信息，缺了页面顶多少几个名字，
    不该让整个接口 500。降级要留日志，否则排障时看不出是"本来没有"还是"读失败"。
    """
    path = stations_file()
    try:
        with open(path, encoding="utf-8") as fp:
            payload = json.load(fp)
    except FileNotFoundError:
        logger.warning("站点表不存在：%s，/api/stations 返回空列表", path)
        return {}
    except (OSError, json.JSONDecodeError):
        logger.warning("站点表读取失败：%s，/api/stations 返回空列表", path, exc_info=True)
        return {}
    if not isinstance(payload, dict):
        logger.warning("站点表格式不是对象，忽略：%s", path)
        return {}
    return payload


def station_entry(station_id, raw):
    """把采集表的单条记录转成契约 4.5 的站点结构。

    缺字段一律给 None 而不是猜：坐标猜错比没有更糟，前端没法判断该不该画。
    """
    if not isinstance(raw, dict):
        raw = {}
    return {
        "station_id": station_id,
        "name": raw.get("name"),
        "line_id": raw.get("line_id"),
        "order": raw.get("order"),
        "lng": raw.get("lng"),
        "lat": raw.get("lat"),
        "color": raw.get("color"),
        # 不对外输出，只在归并线路时用来填 lines[].name，避免拿站名冒充线路名
        "line_name": raw.get("line_name"),
    }


def build_lines(stations):
    """按线路归并站点，顺序取 station_id 排序结果。

    没有可信的站序（采集表还没 order）时，按编码排序是唯一确定的顺序：
    至少每次请求结果一致，不会因为字典顺序变化导致前端图例跳来跳去。
    """
    grouped = {}
    for station in stations:
        line_id = station["line_id"]
        if not line_id:
            continue
        grouped.setdefault(line_id, []).append(station)

    lines = []
    for line_id in sorted(grouped):
        members = sorted(grouped[line_id], key=lambda s: (s["order"] is None, s["order"] or 0,
                                                          s["station_id"]))
        lines.append({
            "line_id": line_id,
            # 线路名只认采集表里显式的 line_name。原先取首个站点的 name 当线路名，
            # 会把"人民广场"标成 1 号线的名字——这是编造，契约 4.5 明确禁止。
            "name": line_name_of(grouped[line_id]),
            "color": next((m["color"] for m in members if m["color"]), None),
            "station_ids": [m["station_id"] for m in members],
        })
    return lines


def line_name_of(members):
    """线路名：只看成员站点上有没有 line_name 字段，没有就给 null。"""
    for member in members:
        name = member.get("line_name")
        if name:
            return name
    return None


def build_line_map():
    """返回 {station_id: line_id}。metric_window 表没有线路列，只能靠这份映射。

    站点表读不到时返回空字典：调用方据此判断"按线路筛"筛不出东西，
    而不是把整条线当成不存在还返回 200 让前端以为线路没数据。
    """
    return {
        station_id: value.get("line_id")
        for station_id, value in load_stations().items()
        if isinstance(value, dict) and value.get("line_id")
    }


def stations_of_line(line_id):
    """某条线路下的所有站点编码。"""
    return [sid for sid, lid in build_line_map().items() if lid == line_id]


def stations_payload():
    """契约 4.5 的完整响应体。

    内部给站点补了 `line_name` 供归并线路用，对外必须剔除：
    契约 4.5 的 station 字段表里没有它，多给就是自创字段。
    """
    raw = load_stations()
    stations = [station_entry(sid, value) for sid, value in sorted(raw.items())]
    lines = build_lines(stations)
    public = [
        {key: value for key, value in station.items() if key != "line_name"}
        for station in stations
    ]
    return {"stations": public, "lines": lines}