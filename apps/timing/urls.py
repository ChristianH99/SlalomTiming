from django.urls import path

from . import views

app_name = "timing"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("timing/settings/", views.TimingSettingsView.as_view(), name="settings"),
    path("timing/simulator/", views.SimulatorView.as_view(), name="simulator"),
    path("timing/signal/", views.timing_signal, name="signal"),
    path("timing/times/", views.TimingLiveView.as_view(), name="times"),
    path("timing/arrangement/", views.timing_arrangement, name="arrangement"),
    path("timing/run/", views.timing_run_update, name="run-update"),
    path("timing/ignore/", views.timing_ignore, name="ignore"),
    path("timing/pair/", views.timing_pair, name="pair"),
]
