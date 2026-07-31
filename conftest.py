import pytest


@pytest.fixture(autouse=True)
def _english_ui(settings):
    """Pin the UI language to English for the test suite. The app now ships with
    German as the default language (LANGUAGE_CODE='de'), so without this the test
    client would render German and assertions on user-facing text ("Difference",
    "Starters:", …) would break. Tests assert behaviour, not translations, so a
    stable English UI keeps them independent of the shipped default. A test that
    specifically checks translation can override the language itself.
    """
    settings.LANGUAGE_CODE = "en"


@pytest.fixture(autouse=True)
def _fixed_time_zone(settings):
    """Pin the test suite's zone, for the same reason as the language above.

    TIME_ZONE is now read from the machine (config.settings._local_time_zone), so
    without this a test asserting on a rendered timestamp would pass on a laptop in
    Europe/Berlin and fail on a CI runner in UTC — or the other way round, which is
    worse, because it looks like the code broke. A test about the real zone sets it
    itself.
    """
    settings.TIME_ZONE = "UTC"


@pytest.fixture(autouse=True)
def _login_superuser(request, django_user_model):
    """Access control now requires a login on every page, so the existing view
    tests (which drive the `client` fixture) need an authenticated session. Log
    in a superuser by default; a test wanting anonymous or role-scoped access can
    use a fresh `django.test.Client()` instead of the shared `client` fixture.

    Skipped for tests that don't touch the database (no `db`/`client` needed) and
    for tests marked `@pytest.mark.no_auto_login`.
    """
    if "django_db" not in {m.name for m in request.node.iter_markers()} and \
            "db" not in request.fixturenames and "client" not in request.fixturenames:
        return None
    if request.node.get_closest_marker("no_auto_login"):
        return None
    user = django_user_model.objects.create_superuser(
        username="auto-test-admin", email="", password="pw-not-used")
    if "client" in request.fixturenames:
        request.getfixturevalue("client").force_login(user)
    return user


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "no_auto_login: don't auto-create/login a superuser for this test",
    )
