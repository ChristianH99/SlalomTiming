import pytest


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
