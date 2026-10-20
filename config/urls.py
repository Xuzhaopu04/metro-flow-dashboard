from django.urls import include, path

urlpatterns = [
    path("", include("api.urls")),
]

# 统一错误结构（契约 5.4）。这两个必须写在根路由模块里，写在 settings 里不生效；
# 而且只有 DEBUG=False 时才会走这里，开发时的报错页仍然是 Django 自带的。
handler404 = "api.views.not_found"
handler500 = "api.views.server_error"
