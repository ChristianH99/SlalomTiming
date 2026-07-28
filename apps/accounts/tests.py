import pytest
from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse

from apps.accounts import pages, throttle
from apps.accounts.models import RoleAccess
from apps.timing.models import TimingSignal

pytestmark = pytest.mark.django_db


# The shared `client` fixture is auto-logged-in as a superuser (see conftest.py);
# these tests use fresh clients so they control who is (or isn't) signed in.
def anon():
    return Client()


def user_with_pages(username, page_keys, superuser=False):
    if superuser:
        return User.objects.create_superuser(username=username, email="", password="pw")
    user = User.objects.create_user(username=username, password="pw")
    if page_keys is not None:
        group = Group.objects.create(name=f"{username}-role")
        RoleAccess.objects.create(group=group, pages=list(page_keys))
        user.groups.add(group)
    return user


# ----- page registry -----

def test_every_page_url_maps_back_to_its_page():
    for key, urls in pages.PAGE_URLS.items():
        for app_name, url_name in urls:
            assert key in pages.pages_for_url(app_name, url_name)


def test_user_pages_is_union_of_roles():
    user = User.objects.create_user(username="u", password="pw")
    g1 = Group.objects.create(name="a")
    g2 = Group.objects.create(name="b")
    RoleAccess.objects.create(group=g1, pages=["participants"])
    RoleAccess.objects.create(group=g2, pages=["results"])
    user.groups.add(g1, g2)
    assert pages.user_pages(user) == {"participants", "results"}


def test_superuser_gets_all_pages():
    su = User.objects.create_superuser(username="s", email="", password="pw")
    assert pages.user_pages(su) == set(pages.PAGE_KEYS)


# ----- middleware gating -----

def test_anonymous_is_redirected_to_login():
    resp = anon().get(reverse("timing:manual"))
    assert resp.status_code == 302
    assert reverse("accounts:login") in resp["Location"]


def test_login_page_reachable_while_anonymous():
    assert anon().get(reverse("accounts:login")).status_code == 200


def test_device_signal_endpoint_not_gated_by_login_redirect():
    # The device door is outside the login gate: an anonymous POST is answered by
    # the view (JSON), never redirected to the HTML login page.
    resp = anon().post(reverse("timing:signal"), data="{}", content_type="application/json")
    assert resp.status_code != 302
    assert resp["Content-Type"].startswith("application/json")


@override_settings(DEBUG=False, TIMING_DEVICE_TOKEN="")
def test_signal_endpoint_closed_to_anonymous_in_production_without_token():
    resp = anon().post(reverse("timing:signal"), data="{}", content_type="application/json")
    assert resp.status_code == 401


@override_settings(DEBUG=False, TIMING_DEVICE_TOKEN="s3cr3t-token")
def test_signal_endpoint_requires_matching_token_in_production():
    c = anon()
    url = reverse("timing:signal")
    assert c.post(url, data="{}", content_type="application/json").status_code == 401
    assert c.post(url, data="{}", content_type="application/json",
                  HTTP_X_DEVICE_TOKEN="wrong").status_code == 401
    # A matching token passes authorisation (the request then fails later on the
    # empty payload / device check, but never on auth — so not 401).
    ok = c.post(url, data="{}", content_type="application/json",
                HTTP_X_DEVICE_TOKEN="s3cr3t-token")
    assert ok.status_code != 401


def test_signal_endpoint_requires_the_timing_page():
    """SEC-2: the device door is outside the login gate, so it used to accept any
    authenticated session — a registration desk could write times into the live
    event. A login is not authorisation; the Timing page is."""
    desk = Client()
    desk.force_login(user_with_pages("desk-only", ["participants"]))
    signal = {"running_number": 1, "port": 1, "time": "10:00:00.000"}
    refused = desk.post(reverse("timing:signal"), data=signal, content_type="application/json")
    assert refused.status_code == 403
    assert not TimingSignal.objects.exists()

    timekeeper = Client()
    timekeeper.force_login(user_with_pages("tk", ["timing"]))
    allowed = timekeeper.post(
        reverse("timing:signal"), data=signal, content_type="application/json"
    )
    assert allowed.status_code == 200
    assert TimingSignal.objects.count() == 1


# ----- SEC-4: failed-login throttling -----

@pytest.fixture
def clean_throttle():
    """The throttle counts in the local-memory cache, which outlives a test."""
    cache.clear()
    yield
    cache.clear()


def _attempt(client, username="tk", password="wrong-password"):
    return client.post(reverse("accounts:login"),
                       {"username": username, "password": password})


@override_settings(LOGIN_MAX_ATTEMPTS=3, LOGIN_LOCKOUT_SECONDS=300)
def test_login_locks_out_after_repeated_failures(clean_throttle):
    User.objects.create_user(username="tk", password="the-real-password")
    c = Client()
    for _ in range(3):
        assert _attempt(c).status_code == 200        # wrong password, form redisplayed

    # Locked out now — and the *right* password is refused too, otherwise the limit
    # could be walked around by guessing until you hit it.
    locked = _attempt(c, password="the-real-password")
    assert locked.status_code == 200
    assert b"Too many failed attempts" in locked.content
    assert not locked.wsgi_request.user.is_authenticated


@override_settings(LOGIN_MAX_ATTEMPTS=3, LOGIN_LOCKOUT_SECONDS=300)
def test_a_good_login_clears_the_count(clean_throttle):
    User.objects.create_user(username="tk", password="the-real-password")
    c = Client()
    _attempt(c)
    _attempt(c)
    assert throttle.failures("tk", "127.0.0.1") == 2
    assert _attempt(c, password="the-real-password").status_code == 302   # signed in
    assert throttle.failures("tk", "127.0.0.1") == 0


@override_settings(LOGIN_MAX_ATTEMPTS=2, LOGIN_LOCKOUT_SECONDS=300)
def test_lockout_is_per_username_and_ip(clean_throttle):
    """One fumbling operator must not lock an account for everyone else, and one
    host must not be able to work through a user list from a single address."""
    User.objects.create_user(username="tk", password="the-real-password")
    User.objects.create_user(username="other", password="another-password")
    c = Client()
    _attempt(c)
    _attempt(c)
    assert throttle.locked_out("tk", "127.0.0.1")
    # Same address, different account: untouched.
    assert not throttle.locked_out("other", "127.0.0.1")
    # Same account, a different device on the venue network: untouched.
    assert not throttle.locked_out("tk", "192.168.1.77")
    elsewhere = Client(REMOTE_ADDR="192.168.1.77")
    assert _attempt(elsewhere, password="the-real-password").status_code == 302


def test_forwarded_for_is_only_trusted_behind_a_proxy(rf):
    """A client can put anything in X-Forwarded-For, so trusting it unconditionally
    would make the throttle key attacker-chosen — i.e. no throttle at all."""
    request = rf.post("/", REMOTE_ADDR="10.0.0.9", HTTP_X_FORWARDED_FOR="1.2.3.4")
    assert throttle.client_ip(request) == "10.0.0.9"
    with override_settings(SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https")):
        # The proxy appends the peer it saw, so the *last* hop is the real one.
        assert throttle.client_ip(request) == "1.2.3.4"


def test_role_scoped_user_allowed_own_page_blocked_others():
    user = user_with_pages("marshal", ["participants"])
    c = Client()
    c.force_login(user)
    assert c.get(reverse("participants:list")).status_code == 200
    assert c.get(reverse("timing:manual")).status_code == 403


def test_superuser_reaches_everything():
    su = user_with_pages("boss", None, superuser=True)
    c = Client()
    c.force_login(su)
    assert c.get(reverse("timing:manual")).status_code == 200
    assert c.get(reverse("accounts:users")).status_code == 200


def test_management_page_is_superuser_only():
    user = user_with_pages("desk", ["participants"])
    c = Client()
    c.force_login(user)
    assert c.get(reverse("accounts:users")).status_code == 403


# ----- management actions -----

def test_superuser_can_create_role_and_user():
    su = user_with_pages("boss", None, superuser=True)
    c = Client()
    c.force_login(su)
    c.post(reverse("accounts:role-create"), {"name": "Timekeepers"})
    group = Group.objects.get(name="Timekeepers")
    c.post(reverse("accounts:role-update"), {"group": group.pk, "pages": ["timing", "results"]})
    assert set(group.access.pages) == {"timing", "results"}
    c.post(reverse("accounts:user-create"),
           {"username": "tk", "password": "sup3r-secret-pw", "roles": [group.pk]})
    created = User.objects.get(username="tk")
    assert list(created.groups.values_list("name", flat=True)) == ["Timekeepers"]


def test_cannot_delete_last_superuser():
    su = user_with_pages("only-boss", None, superuser=True)
    c = Client()
    c.force_login(su)
    c.post(reverse("accounts:user-delete"), {"user": su.pk})
    assert User.objects.filter(pk=su.pk).exists()


# --- SEC-F: one host must not be able to walk a user list -------------------

class TestHostThrottle:
    """The per-(username, IP) counter alone left an address free to try ten
    passwords against each of a hundred accounts. An address has its own budget."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        from django.core.cache import cache
        cache.clear()
        yield
        cache.clear()

    def test_many_usernames_from_one_address_are_eventually_refused(self, settings):
        settings.LOGIN_MAX_ATTEMPTS = 10
        settings.LOGIN_MAX_ATTEMPTS_PER_HOST = 6
        client = Client()
        # Six different accounts, one wrong guess each: under the per-pair limit
        # every time, so nothing here would have tripped before.
        for i in range(6):
            client.post(reverse("accounts:login"),
                        {"username": f"victim{i}", "password": "wrong"})
        response = client.post(reverse("accounts:login"),
                               {"username": "victim99", "password": "wrong"})
        assert response.context["locked_out"] is True

    def test_a_good_password_does_not_clear_the_address_budget(self, settings, django_user_model):
        settings.LOGIN_MAX_ATTEMPTS_PER_HOST = 3
        django_user_model.objects.create_user(username="operator", password="Zx9!qwerty-long")
        client = Client()
        for i in range(3):
            client.post(reverse("accounts:login"),
                        {"username": f"other{i}", "password": "wrong"})
        client.post(reverse("accounts:login"),
                    {"username": "operator", "password": "Zx9!qwerty-long"})
        response = client.post(reverse("accounts:login"),
                               {"username": "someone", "password": "wrong"})
        assert response.context["locked_out"] is True


# --- SEC-L / SEC-M / SEC-N: managing accounts -------------------------------

class TestAccountManagement:
    def test_resetting_your_own_password_does_not_sign_you_out(self, client, django_user_model):
        admin = django_user_model.objects.create_superuser(
            username="boss", email="", password="Zx9!qwerty-long")
        client.force_login(admin)
        client.post(reverse("accounts:user-update"),
                    {"user": admin.pk, "password": "Nw7!qwerty-longer"})
        # Still signed in: set_password rotates the hash the session is signed
        # against, so without update_session_auth_hash the very next request is
        # anonymous.
        assert client.get(reverse("accounts:users")).status_code == 200

    def test_a_deactivated_account_cannot_sign_in(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="marshal", password="Zx9!qwerty-long")
        admin = django_user_model.objects.create_superuser(
            username="boss", email="", password="x")
        client.force_login(admin)
        client.post(reverse("accounts:user-set-active"), {"user": user.pk, "active": "0"})
        user.refresh_from_db()
        assert user.is_active is False
        assert django_user_model.objects.filter(pk=user.pk).exists()  # not deleted
        fresh = Client()
        fresh.post(reverse("accounts:login"),
                   {"username": "marshal", "password": "Zx9!qwerty-long"})
        assert fresh.get("/").status_code == 302  # still anonymous

    def test_the_last_active_superuser_cannot_deactivate_themselves(self, client, django_user_model):
        admin = django_user_model.objects.create_superuser(
            username="boss", email="", password="x")
        client.force_login(admin)
        client.post(reverse("accounts:user-set-active"), {"user": admin.pk, "active": "0"})
        admin.refresh_from_db()
        assert admin.is_active is True

    def test_a_signed_in_non_superuser_is_refused_not_redirected(self, django_user_model):
        """A redirect to the login page asks "who are you?" of somebody who is
        already signed in; the answer is "not you"."""
        django_user_model.objects.create_user(username="plain", password="Zx9!qwerty-long")
        client = Client()
        client.login(username="plain", password="Zx9!qwerty-long")
        response = client.post(reverse("accounts:user-create"),
                               {"username": "x", "password": "Zx9!qwerty-long"})
        assert response.status_code == 403
