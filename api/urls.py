from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("api/snapshot", views.snapshot, name="snapshot"),
    path("api/series", views.series, name="series"),
    path("api/rank", views.rank, name="rank"),
]
