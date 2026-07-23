import pytest
from django.contrib.auth.models import Group, User
from django.test import Client, override_settings
from django.urls import reverse

from apps.accounts import pages
from apps.accounts.models import RoleAccess

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
