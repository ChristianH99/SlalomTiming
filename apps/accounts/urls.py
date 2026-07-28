from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    # Our own LoginView: Django's has no failed-attempt limit (see throttle.py).
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("users/", views.UserAccessView.as_view(), name="users"),
    # The audit trail (SEC-9). Under the accounts app because the middleware makes
    # this whole app superuser-only — see apps/accounts/middleware.py.
    path("audit-log/", views.audit_log, name="audit-log"),
    path("roles/create/", views.role_create, name="role-create"),
    path("roles/update/", views.role_update, name="role-update"),
    path("roles/delete/", views.role_delete, name="role-delete"),
    path("users/create/", views.user_create, name="user-create"),
    path("users/update/", views.user_update, name="user-update"),
    path("users/active/", views.user_set_active, name="user-set-active"),
    path("users/delete/", views.user_delete, name="user-delete"),
]
