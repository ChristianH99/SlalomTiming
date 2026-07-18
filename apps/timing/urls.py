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
    path("timing/run/add/", views.timing_add_run, name="run-add"),
    path("timing/run/delete/", views.timing_delete_run, name="run-delete"),
    path("timing/ignore/", views.timing_ignore, name="ignore"),
    path("timing/pair/", views.timing_pair, name="pair"),
    path("timing/auto/", views.AutoTimingView.as_view(), name="auto"),
    path("timing/auto/state/", views.auto_arrangement, name="auto-state"),
    path("timing/auto/reorder/", views.auto_reorder, name="auto-reorder"),
    path("timing/auto/reset-order/", views.auto_reset_order, name="auto-reset-order"),
    path("timing/marshal/state/", views.marshal_state, name="marshal-state"),
    path("timing/marshal/submit/", views.marshal_submit, name="marshal-submit"),
]
