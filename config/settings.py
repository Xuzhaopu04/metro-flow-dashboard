"""Django 配置。

所有与环境相关的值都从环境变量读，默认值只够本地开发用。
账号密码、Redis 地址这类东西不进代码，统一放 .env（见 .env.example）。
"""

import logging
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-do-not-use-in-production")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "api",
]

MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # web/ 既是页面源码目录也是模板目录，这样页面和接口同源，不用配 CORS
        "DIRS": [BASE_DIR / "web"],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": os.environ.get("MYSQL_DB", "metro"),
        "USER": os.environ.get("MYSQL_USER", "root"),
        "PASSWORD": os.environ.get("MYSQL_PASSWORD", "metro_dev_pwd"),
        "HOST": os.environ.get("MYSQL_HOST", "127.0.0.1"),
        "PORT": os.environ.get("MYSQL_PORT", "3306"),
        "OPTIONS": {"charset": "utf8mb4"},
    }
}

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---- 业务参数（采集端、计算端、接口端共用同一批环境变量名）----
REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))

EVENT_STREAM = os.environ.get("EVENT_STREAM", "gate_events")
CONSUMER_GROUP = os.environ.get("CONSUMER_GROUP", "metric_builder")
SNAPSHOT_KEY = os.environ.get("SNAPSHOT_KEY", "metric:snapshot")

WINDOW_SECONDS = int(os.environ.get("WINDOW_SECONDS", "1"))
SNAPSHOT_TTL = int(os.environ.get("SNAPSHOT_TTL", "5"))
# 迟到容差：事件时间早于当前窗口 2 个窗口以上就算迟到
LATE_TOLERANCE_WINDOWS = int(os.environ.get("LATE_TOLERANCE_WINDOWS", "2"))

# 单窗口客流告警阈值（人次/秒）。原先写死在页面里，改阈值要改代码；
# 现在统一由服务端读取，页面向 /api/alert 取值，改 .env 即可生效。
ALERT_THRESHOLD_DEFAULT = 100


def parse_positive_int(raw, default, name):
    """把环境变量解析成正整数，解析不了就退回默认值。

    这里不用裸 int()：环境变量写错（比如 ALERT_THRESHOLD=abc）会让整个 Django
    在导入阶段就崩掉，一个可选参数不该有这种杀伤力。宁可降级跑起来并留一条日志，
    也不要整个服务起不来——页面拿默认阈值照样能用。
    """
    text = (raw or "").strip()
    # .env 里写成 "ALERT_THRESHOLD=" 拿到的是空串，那是"没配"不是"配错了"，不必惊动日志
    if not text:
        return default
    try:
        value = int(text)
    except ValueError:
        logging.getLogger(__name__).warning(
            "%s 不是整数（收到 %r），已回退为默认值 %s", name, text, default
        )
        return default
    # 0 或负数会让"客流 ≥ 阈值"永远成立，横幅一直挂着，没有任何告警含义
    if value < 1:
        logging.getLogger(__name__).warning(
            "%s 必须为正整数，收到 %s，已回退为默认值 %s", name, value, default
        )
        return default
    return value


ALERT_THRESHOLD = parse_positive_int(
    os.environ.get("ALERT_THRESHOLD"), ALERT_THRESHOLD_DEFAULT, "ALERT_THRESHOLD"
)
