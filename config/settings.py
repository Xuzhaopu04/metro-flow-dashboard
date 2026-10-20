"""Django 配置。

所有与环境相关的值都从环境变量读，默认值只够本地开发用。
账号密码、Redis 地址这类东西不进代码，统一放 .env（见 .env.example）。
"""

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
