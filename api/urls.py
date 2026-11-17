from django.urls import path, re_path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("api/snapshot", views.snapshot, name="snapshot"),
    path("api/series", views.series, name="series"),
    path("api/rank", views.rank, name="rank"),
    # 契约 v1.3 新增的两个只读接口
    path("api/summary/today", views.summary_today, name="summary_today"),
    path("api/stations", views.stations, name="stations"),
    # 兜底放最后：DEBUG=True 时 Django 不走 handler404，而是返回 HTML 报错页，
    # 但契约第 1 节要求错误体是 JSON。这条只接 /api/ 开头且前面都没匹配上的路径，
    # 让开发环境与演示环境的错误格式一致；页面路径仍交给 Django 自己处理。
    re_path(r"^api/.*$", views.not_found, name="api_not_found"),
]
