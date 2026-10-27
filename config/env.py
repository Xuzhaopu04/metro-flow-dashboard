"""加载项目根目录的 .env。

docker compose 自带 .env 解析，Python 侧没有；这里补一个最小实现，
避免为读一个文件新增 python-dotenv 依赖。
"""

import os
from pathlib import Path

# 按文件位置定位，不依赖启动目录，换 cwd 也能找到项目根
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"


def load_env(path=ENV_FILE):
    """把 .env 里未被真实环境变量占用的键补进 os.environ；文件不存在就跳过。"""
    try:
        with open(path, encoding="utf-8") as fp:
            lines = fp.readlines()
    except OSError:
        # CI 与容器里通常没有 .env，属于正常情况，静默跳过
        return

    for raw in lines:
        line = raw.strip()
        # 空行、# 注释、没有 = 的行都不是配置，直接跳过
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        # 值两侧成对的单/双引号只是包裹，去掉；不成对的原样保留
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        # 关键：真实环境变量 > .env > 代码默认值，容器里 compose 注入的地址不能被覆盖
        os.environ.setdefault(key, value)
