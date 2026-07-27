from django.urls import path

from . import views

app_name = "timing"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("dashboard/state/", views.dashboard_state, name="dashboard-state"),
    path("timing/settings/", views.TimingSettingsView.as_view(), name="settings"),
    path("timing/cp540/status/", views.cp540_status, name="cp540-status"),
    path("timing/input-lock/", views.timing_input_lock, name="input-lock"),
    path("timing/simulator/", views.SimulatorView.as_view(), name="simulator"),
    path("timing/signal/", views.timing_signal, name="signal"),
    path("timing/manual/", views.TimingLiveView.as_view(), name="manual"),
    path("timing/arrangement/", views.timing_arrangement, name="arrangement"),
    path("timing/run/", views.timing_run_update, name="run-update"),
    path("timing/run/add/", views.timing_add_run, name="run-add"),
    path("timing/run/delete/", views.timing_delete_run, name="run-delete"),
    path("timing/run/status/", views.timing_run_status, name="run-status"),
    path("timing/ignore/", views.timing_ignore, name="ignore"),
    path("timing/pair/", views.timing_pair, name="pair"),
    path("timing/set-time/", views.timing_set_time, name="set-time"),
    path("timing/set-runtime/", views.timing_set_runtime, name="set-runtime"),
    path("timing/auto/", views.AutoTimingView.as_view(), name="auto"),
    path("timing/auto/state/", views.auto_arrangement, name="auto-state"),
    path("timing/auto/reorder/", views.auto_reorder, name="auto-reorder"),
    path("timing/auto/reset-order/", views.auto_reset_order, name="auto-reset-order"),
    path("timing/marshal/state/", views.marshal_state, name="marshal-state"),
    path("timing/marshal/submit/", views.marshal_submit, name="marshal-submit"),
    path("timing/marshal/unlock/", views.marshal_unlock, name="marshal-unlock"),
    path("timing/marshal/lock/", views.marshal_lock, name="marshal-lock"),
    path("timing/marshal/lock-all/", views.marshal_lock_all, name="marshal-lock-all"),
    path("timing/marshal/task-edit/", views.marshal_task_edit, name="marshal-task-edit"),
    path("timing/marshal/claim/", views.marshal_claim, name="marshal-claim"),
    path("timing/marshal/release/", views.marshal_release, name="marshal-release"),
    path("timing/marshal/claims/", views.marshal_claims, name="marshal-claims"),
    path("timing/auto/adjust/", views.auto_penalty_adjust, name="auto-adjust"),
]
