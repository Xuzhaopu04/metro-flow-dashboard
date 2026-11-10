from django.urls import include, path

urlpatterns = [
    path("", include("api.urls")),
]

# 统一错误结构（契约第 1 节）。这两个必须写在根路由模块里，写在 settings 里不生效。
#
# 生效条件要注意：handler404 只在 DEBUG=False 时被调用。DEBUG=True 时 Django
# 用技术性 404 页（HTML）接管，契约要求的 JSON 就丢了。
# 所以接口路径的 404 不能只靠它——api/urls.py 末尾还有一条兜底路由，
# 让 /api/ 下的未匹配路径在开发环境也返回契约格式。
handler404 = "api.views.not_found"
handler500 = "api.views.server_error"
