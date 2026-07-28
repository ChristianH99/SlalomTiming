import logging
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash, views as auth_views
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.models import Group, User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponseForbidden, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.http import content_disposition_header
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView

from . import pages, throttle
from .models import RoleAccess

logger = logging.getLogger("apps.accounts.login")


def _as_pk(value):
    """A pk from a form field, or None — so a non-numeric one 404s rather than
    raising while Django prepares the query (see the timing endpoints)."""
    text = str(value or "").strip()
    return int(text) if text.isascii() and text.isdigit() else None


class LoginView(auth_views.LoginView):
    """The login form, with a failed-attempt limit per (username, IP).

    Plain LoginView allows unlimited guessing, and on an open venue network that is
    the whole attack. The counting and the logging live in throttle.py; this view is
    only the three places it hooks in: refuse while locked out, count a failure, and
    forget the count on success.
    """

    template_name = "accounts/login.html"

    def post(self, request, *args, **kwargs):
        username = request.POST.get("username", "")
        if throttle.locked_out(username, throttle.client_ip(request)):
            throttle.note_lockout(request, username)
            # The same page the wrong-password path renders, with its own message —
            # and no attempt made, so a locked-out guesser learns nothing about
            # whether the password was right.
            return self.render_to_response(
                self.get_context_data(form=self.get_form(), locked_out=True)
            )
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        throttle.note_success(self.request, form.get_user().get_username())
        return super().form_valid(form)

    def form_invalid(self, form):
        throttle.record_failure(self.request, self.request.POST.get("username", ""))
        return super().form_invalid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("locked_out", False)
        context["lockout_minutes"] = max(1, round(throttle.lockout_seconds() / 60))
        return context


class SuperuserRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Management surface — superusers only. The middleware already enforces this
    for the whole accounts app; this is defence in depth on the view itself."""

    def test_func(self):
        return self.request.user.is_superuser


class UserAccessView(SuperuserRequiredMixin, TemplateView):
    template_name = "accounts/user_access.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        roles = []
        for group in Group.objects.order_by("name"):
            access, _ = RoleAccess.objects.get_or_create(group=group)
            granted = set(access.pages or [])
            roles.append({
                "group": group,
                "pages": [
                    {"key": key, "label": label, "granted": key in granted}
                    for key, label in pages.PAGES
                ],
            })
        users = []
        for user in User.objects.order_by("username").prefetch_related("groups"):
            users.append({
                "user": user,
                "role_ids": {g.pk for g in user.groups.all()},
            })
        # What the audit trail currently holds, so the page can say whether there
        # is anything to download rather than offering an empty file.
        live = Path(settings.AUDIT_LOG_FILE)
        parts = [live, *(live.with_name(f"{live.name}.{n}")
                         for n in range(1, settings.AUDIT_LOG_BACKUPS + 1))]
        present = [p for p in parts if p.exists()]
        context["audit_bytes"] = sum(p.stat().st_size for p in present)
        context["audit_files"] = len(present)
        context["roles"] = roles
        context["users"] = users
        context["all_pages"] = pages.PAGES
        context["all_roles"] = Group.objects.order_by("name")
        return context


def _refused(request):
    """None when the caller may manage users, else the response to return.

    A signed-in non-superuser gets 403, not a redirect to the login page: they are
    already logged in, so sending them back to a form they have nothing to type
    into said "who are you?" when the answer is "not you"."""
    if request.user.is_authenticated and request.user.is_superuser:
        return None
    if request.user.is_authenticated:
        return HttpResponseForbidden()
    return redirect("accounts:login")


# ----- audit trail -----

def audit_log(request):
    """Download the audit trail (SEC-9).

    Superuser-only, and on this page because this page is already the one place
    only a superuser can open — a "Logs" page of its own would need its own key in
    the access registry and its own role, for one download.

    The whole trail, oldest first: the rotated files then the live one, so what
    comes out is one continuous record rather than the last few megabytes. It is
    streamed and never held whole in memory — the budget is five files of 5 MB.
    """
    refused = _refused(request)
    if refused is not None:
        return refused

    live = Path(settings.AUDIT_LOG_FILE)
    # RotatingFileHandler numbers backups newest-first (.1 is the most recent), so
    # reading them in reverse gives chronological order.
    parts = [live.with_name(f"{live.name}.{n}")
             for n in range(settings.AUDIT_LOG_BACKUPS, 0, -1)]
    parts.append(live)
    parts = [p for p in parts if p.exists()]
    if not parts:
        messages.info(request, _("There is nothing in the audit log yet."))
        return redirect("accounts:users")

    def stream():
        for part in parts:
            with part.open("rb") as handle:
                while chunk := handle.read(64 * 1024):
                    yield chunk

    name = f"slalomtiming-audit-{timezone.localdate():%Y%m%d}.log"
    response = StreamingHttpResponse(stream(), content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = content_disposition_header(True, name)
    logger.info(
        "user=%s ip=%s downloaded the audit log (%d file(s))",
        request.user.get_username(), throttle.client_ip(request), len(parts),
    )
    return response


# ----- roles -----

@require_POST
def role_create(request):
    refused = _refused(request)
    if refused is not None:
        return refused
    name = (request.POST.get("name") or "").strip()
    if not name:
        messages.error(request, _("A role needs a name."))
    elif Group.objects.filter(name=name).exists():
        messages.error(request, _("A role named “%(name)s” already exists.") % {"name": name})
    else:
        group = Group.objects.create(name=name)
        RoleAccess.objects.create(group=group, pages=[])
        messages.success(request, _("Role “%(name)s” created.") % {"name": name})
    return redirect("accounts:users")


@require_POST
def role_update(request):
    refused = _refused(request)
    if refused is not None:
        return refused
    group = get_object_or_404(Group, pk=_as_pk(request.POST.get("group")))
    selected = [k for k in request.POST.getlist("pages") if k in pages.PAGE_KEYS]
    access, _created = RoleAccess.objects.get_or_create(group=group)
    access.pages = selected
    access.save(update_fields=["pages"])
    messages.success(request, _("Access for “%(name)s” updated.") % {"name": group.name})
    return redirect("accounts:users")


@require_POST
def role_delete(request):
    refused = _refused(request)
    if refused is not None:
        return refused
    group = get_object_or_404(Group, pk=_as_pk(request.POST.get("group")))
    name = group.name
    group.delete()
    messages.success(request, _("Role “%(name)s” deleted.") % {"name": name})
    return redirect("accounts:users")


# ----- users -----

def _selected_groups(request):
    ids = request.POST.getlist("roles")
    return list(Group.objects.filter(pk__in=ids))


@require_POST
def user_create(request):
    refused = _refused(request)
    if refused is not None:
        return refused
    username = (request.POST.get("username") or "").strip()
    password = request.POST.get("password") or ""
    if not username:
        messages.error(request, _("A username is required."))
        return redirect("accounts:users")
    if User.objects.filter(username=username).exists():
        messages.error(request, _("A user named “%(name)s” already exists.") % {"name": username})
        return redirect("accounts:users")
    try:
        validate_password(password)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("accounts:users")
    with transaction.atomic():
        user = User.objects.create_user(username=username, password=password)
        user.groups.set(_selected_groups(request))
    messages.success(request, _("User “%(name)s” created.") % {"name": username})
    return redirect("accounts:users")


@require_POST
def user_update(request):
    refused = _refused(request)
    if refused is not None:
        return refused
    user = get_object_or_404(User, pk=_as_pk(request.POST.get("user")))
    # A superuser's roles don't affect their (total) access, so leave them alone.
    if not user.is_superuser:
        user.groups.set(_selected_groups(request))
    password = request.POST.get("password") or ""
    if password:
        try:
            validate_password(password, user=user)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect("accounts:users")
        user.set_password(password)
        user.save()
        if user == request.user:
            # Changing a password rotates the hash the session is signed against,
            # so without this the superuser resetting their *own* password is
            # logged straight out by their own next request.
            update_session_auth_hash(request, user)
        messages.success(request, _("Password for “%(name)s” reset.") % {"name": user.username})
    else:
        messages.success(request, _("Roles for “%(name)s” updated.") % {"name": user.username})
    return redirect("accounts:users")


@require_POST
def user_set_active(request):
    """Switch an account on or off without deleting it.

    Deleting was the only way to take someone's access away, which is the wrong
    tool during an event: a marshal who has gone home, a laptop that walked off,
    a volunteer who is not back next weekend. Django refuses a login for an
    inactive user, and this leaves the account (and the audit trail of what it
    did) intact.
    """
    refused = _refused(request)
    if refused is not None:
        return refused
    user = get_object_or_404(User, pk=_as_pk(request.POST.get("user")))
    active = request.POST.get("active") == "1"
    if user == request.user and not active:
        messages.error(request, _("You can’t deactivate your own account."))
        return redirect("accounts:users")
    if user.is_superuser and not active and             User.objects.filter(is_superuser=True, is_active=True).count() <= 1:
        messages.error(request, _("Can’t deactivate the last superuser."))
        return redirect("accounts:users")
    user.is_active = active
    user.save(update_fields=["is_active"])
    if active:
        messages.success(request, _("“%(name)s” can sign in again.") % {"name": user.username})
    else:
        messages.success(
            request,
            _("“%(name)s” is deactivated and can no longer sign in.") % {"name": user.username},
        )
    return redirect("accounts:users")


@require_POST
def user_delete(request):
    refused = _refused(request)
    if refused is not None:
        return refused
    user = get_object_or_404(User, pk=_as_pk(request.POST.get("user")))
    if user == request.user:
        messages.error(request, _("You can’t delete your own account."))
        return redirect("accounts:users")
    if user.is_superuser and User.objects.filter(is_superuser=True).count() <= 1:
        messages.error(request, _("Can’t delete the last superuser."))
        return redirect("accounts:users")
    name = user.username
    user.delete()
    messages.success(request, _("User “%(name)s” deleted.") % {"name": name})
    return redirect("accounts:users")
