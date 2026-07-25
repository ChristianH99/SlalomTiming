"""Failed-login throttling.

The venue network is open enough that marshals' phones join it, so the login form
is reachable by anything on that Wi-Fi. Unlimited guessing against accounts whose
passwords an organiser chose in a hurry on race morning is not an acceptable
default, and Django's LoginView has no rate limit, no lockout and no logging of its
own.

Deliberately small: a counter per (username, IP) in the cache, and a lockout once it
runs over. No new dependency (django-axes brings a model, migrations and an admin),
and nothing that can wedge a legitimate operator out of a running event for long —
the lockout is minutes, and both numbers come from the environment so a stuck
timekeeper can be let back in without a code change.

Consequences worth knowing:

* The counters live in the local-memory cache, so a server restart clears them. The
  app already runs as exactly one process (config/singleinstance.py), so per-process
  state is per-system state here.
* Keyed on username *and* IP: one fumbling operator can't lock an account for
  everybody else, and one host can't work through a user list from a single address.
* Every failure is logged with the username and IP — SEC-4 was as much about having
  nothing to look at afterwards as about the guessing itself.
"""

import logging

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger("apps.accounts.login")

CACHE_PREFIX = "login-fail:"


def max_attempts():
    return getattr(settings, "LOGIN_MAX_ATTEMPTS", 10)


def lockout_seconds():
    return getattr(settings, "LOGIN_LOCKOUT_SECONDS", 300)


def client_ip(request):
    """The requesting address. X-Forwarded-For is only trusted for its *last* hop
    when a proxy is configured (SECURE_PROXY_SSL_HEADER), because a client can send
    whatever it likes in that header — a spoofable key would make the throttle
    trivial to sidestep."""
    if getattr(settings, "SECURE_PROXY_SSL_HEADER", None):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def _key(username, ip):
    return f"{CACHE_PREFIX}{(username or '').strip().lower()}|{ip}"


def failures(username, ip):
    return cache.get(_key(username, ip), 0)


def locked_out(username, ip):
    """Whether this (username, IP) pair has used up its attempts."""
    return failures(username, ip) >= max_attempts()


def record_failure(request, username):
    """Count one failed attempt and log it. Returns the new count."""
    ip = client_ip(request)
    key = _key(username, ip)
    # add() then incr(): incr on a missing key raises, and add() is the atomic way
    # to seed it without clobbering a count set a moment earlier.
    if cache.add(key, 1, lockout_seconds()):
        count = 1
    else:
        try:
            count = cache.incr(key)
        except ValueError:  # expired between the add and the incr
            cache.set(key, 1, lockout_seconds())
            count = 1
    logger.warning(
        "Failed login for %r from %s (%d/%d)", username, ip, count, max_attempts()
    )
    return count


def clear(request, username):
    """Forget the failures for this pair — called on a successful login."""
    cache.delete(_key(username, client_ip(request)))


def note_success(request, username):
    clear(request, username)
    logger.info("Login for %r from %s", username, client_ip(request))


def note_lockout(request, username):
    logger.warning(
        "Login refused (throttled) for %r from %s", username, client_ip(request)
    )
