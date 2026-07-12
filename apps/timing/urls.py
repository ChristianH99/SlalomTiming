from django.urls import path

from . import views

app_name = "timing"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
]
