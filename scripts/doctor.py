#!/usr/bin/env python3
"""本地排障自检：一条命令把页面依赖的每一环查一遍。

用法：python scripts/doctor.py
只读检查，不改状态；有问题时在末尾打印下一步该执行的命令。
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = "http://127.0.0.1:8000"
TIMEOUT = 3
RESULTS = []   # (级别, 检查项, 说明)


def record(level, title, detail=""):
    RESULTS.append((level, title, detail))


def sh(*args):
    """跑外部命令，统一返回 (退出码, stdout, stderr)。"""
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=15)
        return done.returncode, done.stdout.strip(), done.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def get_json(path):
    """请求接口，返回 (HTTP 码, 解析后的对象, 错误说明, 耗时毫秒)。"""
    started = time.monotonic()
    try:
        with urllib.request.urlopen(BASE + path, timeout=TIMEOUT) as resp:
            body, code = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        return exc.code, None, "HTTP %d" % exc.code, 0
    except (urllib.error.URLError, OSError) as exc:
        return 0, None, str(exc), 0
    cost = (time.monotonic() - started) * 1000
    try:
        return code, json.loads(body), "", cost
    except json.JSONDecodeError:
        return code, None, "响应不是 JSON", cost


def check_containers():
    for name in ("metro-mysql", "metro-redis"):
        code, out, err = sh("docker", "inspect", "-f", "{{.State.Health.Status}}", name)
        if code != 0:
            record("bad", "容器 " + name, err or "docker 不可用")
        elif out != "healthy":
            record("bad", "容器 " + name, "状态 " + (out or "unknown"))
        else:
            record("ok", "容器 " + name, "healthy")


def check_processes():
    """进程在不在。ps 的输出里带完整命令行，够判断了。"""
    code, out, err = sh("ps", "-eo", "args")
    if code != 0:
        record("bad", "进程列表", err)
        return
    for title, pattern in (("消费者", "stream/consumer.py"),
                           ("模拟器", "simulator/simulator.py"),
                           ("Web 服务", "manage.py runserver")):
        alive = any(pattern in line and "doctor.py" not in line
                    for line in out.splitlines())
        record("ok" if alive else "bad", "进程 " + title, "运行中" if alive else "没找到")


def check_snapshot_shape(data):
    """快照是全页面的数据源，字段形状变了前端会整片停摆。"""
    if not isinstance(data, dict):
        return "响应不是对象"
    missing = [k for k in ("ts", "total", "stations", "late_count",
                           "invalid_count", "alerting", "threshold") if k not in data]
    if missing:
        return "缺字段 " + ", ".join(missing)
    if not isinstance(data.get("stations"), list):
        return "stations 不是数组"
    for row in data["stations"]:
        if not isinstance(row, dict) or "station_id" not in row:
            return "stations 里有残缺条目"
        if not isinstance(row.get("passengers"), int):
            return "%s 的 passengers 不是整数" % row.get("station_id")
    return ""


def snapshot_age(data):
    try:
        stamp = datetime.fromisoformat(data["ts"])
    except (KeyError, TypeError, ValueError):
        return None
    return max(0, int((datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()))


def check_stations_shape(data):
    """站点表这层最容易出问题：契约要求扁平，采集端却是嵌套结构。"""
    if not isinstance(data, dict) or not isinstance(data.get("stations"), list):
        return "stations 不是数组"
    stations = data["stations"]
    if not stations:
        return "站点列表为空"
    broken = [s for s in stations if not isinstance(s, dict) or not s.get("line_id")]
    if broken:
        ids = ", ".join(str(s.get("station_id")) for s in broken[:3])
        return "这些条目没有 line_id：" + ids + "（多半是读错了 json 层级）"
    return ""


def check_list_shape(field):
    def validate(data):
        if not isinstance(data, dict) or not isinstance(data.get(field), list):
            return "%s 不是数组" % field
        return ""
    return validate


def check_endpoints():
    cases = [
        ("/api/snapshot", check_snapshot_shape),
        ("/api/stations", check_stations_shape),
        ("/api/rank?top=5", check_list_shape("top")),
        ("/api/series?minutes=5", check_list_shape("points")),
        ("/api/summary/today", lambda d: "" if isinstance(d, dict) and "total" in d else "缺 total"),
    ]
    for path, validate in cases:
        code, data, err, cost = get_json(path)
        if code != 200:
            record("bad", "接口 " + path, err or "HTTP %d" % code)
            continue
        problem = validate(data)
        if problem:
            record("bad", "接口 " + path, problem)
        else:
            record("ok", "接口 " + path, "200，%.0f ms" % cost)
        if path == "/api/snapshot" and not problem:
            age = snapshot_age(data)
            if age is None:
                record("warn", "快照时间", "ts 解析不了：" + str(data.get("ts")))
            elif age > 10:
                record("warn", "快照时间", "已经 %d 秒没更新" % age)


def stream_length():
    code, out, _ = sh("docker", "exec", "metro-redis", "redis-cli", "XLEN", "gate_events")
    return int(out) if code == 0 and out.isdigit() else None


def check_redis():
    first = stream_length()
    if first is None:
        record("bad", "事件流 gate_events", "读不到长度，Redis 可能没起")
        return
    time.sleep(2)
    second = stream_length()
    if second is None:
        record("bad", "事件流 gate_events", "第二次读取失败")
    elif second > first:
        record("ok", "事件流 gate_events", "2 秒新增 %d 条" % (second - first))
    else:
        record("bad", "事件流 gate_events", "2 秒没有新增，采集端或消费者停了")


def print_report():
    marks = {"ok": "[OK]  ", "warn": "[WARN]", "bad": "[FAIL]"}
    for level, title, detail in RESULTS:
        print("%s %-28s %s" % (marks[level], title, detail))
    bad = sum(1 for item in RESULTS if item[0] == "bad")
    warn = sum(1 for item in RESULTS if item[0] == "warn")
    print()
    print("结果：%d 项通过，%d 项异常，%d 项提醒" %
          (len(RESULTS) - bad - warn, bad, warn))
    if bad:
        print("下一步：先按失败的项修；页面还是旧的就硬刷新 Ctrl+Shift+R。")
    else:
        print("依赖都正常。页面异常就是前端本身，打开 F12 看第一条红错。")
    return 1 if bad else 0


def main():
    check_containers()
    check_processes()
    check_endpoints()
    check_redis()
    return print_report()


if __name__ == "__main__":
    sys.exit(main())
