from django.urls import path

from . import views

app_name = "results"

urlpatterns = [
    path("settings/", views.ResultsSettingsView.as_view(), name="settings"),
    path("<int:pk>/", views.ResultsClassView.as_view(), name="class"),
]
