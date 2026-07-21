from django.urls import path

from . import views

app_name = "results"

urlpatterns = [
    path("", views.ResultsIndexView.as_view(), name="index"),
    path("settings/", views.ResultsSettingsView.as_view(), name="settings"),
    path("tie/", views.ResultsTieResolveView.as_view(), name="tie-resolve"),
    path("overall/<str:method>/<int:runs>/", views.ResultsOverallView.as_view(), name="overall"),
    path("<int:pk>/", views.ResultsClassView.as_view(), name="class"),
]
