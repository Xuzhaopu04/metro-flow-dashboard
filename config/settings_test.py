"""测试专用配置：用内存 SQLite 代替 MySQL，跑测试不用起数据库。

pytest.ini 里已经指向这个模块，`pytest` 直接就能跑。
"""

import os

from config.settings import *  # noqa: F401,F403

DEBUG = False

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# 指到一个没人监听的端口，让接口测试快速拿到连接失败，而不是每次等 2 秒超时
REDIS_PORT = int(os.environ.get("TEST_REDIS_PORT", "1"))
SNAPSHOT_TTL = 5
