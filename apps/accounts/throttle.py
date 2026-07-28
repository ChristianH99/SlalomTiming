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
* Two counters, not one. Per (username, IP), so one fumbling operator can't lock
  an account for everybody else; and per IP alone, because the pair counter on its
  own let a single host walk a user list — ten guesses at each of a hundred names
  trips nothing, and a guess at a name that doesn't exist is free.
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


def host_max_attempts():
    """Failures allowed from one address across *all* usernames.

    The per-pair counter alone lets one host work through a user list unimpeded:
    ten guesses at each of a hundred names is a thousand attempts and never trips
    anything, and a wrong guess at a username that doesn't exist costs nothing at
    all. So an address has a budget of its own. Set well above the per-pair limit,
    because one venue laptop may legitimately be several people's browser.
    """
    return getattr(settings, "LOGIN_MAX_ATTEMPTS_PER_HOST", max_attempts() * 5)


def _key(username, ip):
    return f"{CACHE_PREFIX}{(username or '').strip().lower()}|{ip}"


def _host_key(ip):
    return f"{CACHE_PREFIX}host|{ip}"


def failures(username, ip):
    return cache.get(_key(username, ip), 0)


def host_failures(ip):
    return cache.get(_host_key(ip), 0)


def locked_out(username, ip):
    """Whether this attempt is refused — because the (username, IP) pair has used
    up its attempts, or because the address as a whole has."""
    return (
        failures(username, ip) >= max_attempts()
        or host_failures(ip) >= host_max_attempts()
    )


def _bump(key):
    """Increment a counter that may not exist yet, and return its new value."""
    # add() then incr(): incr on a missing key raises, and add() is the atomic way
    # to seed it without clobbering a count set a moment earlier.
    if cache.add(key, 1, lockout_seconds()):
        return 1
    try:
        return cache.incr(key)
    except ValueError:  # expired between the add and the incr
        cache.set(key, 1, lockout_seconds())
        return 1


def record_failure(request, username):
    """Count one failed attempt and log it. Returns the new per-pair count."""
    ip = client_ip(request)
    count = _bump(_key(username, ip))
    from_host = _bump(_host_key(ip))
    logger.warning(
        "Failed login for %r from %s (%d/%d for this account, %d/%d from this address)",
        username, ip, count, max_attempts(), from_host, host_max_attempts(),
    )
    return count


def clear(request, username):
    """Forget the failures for this pair — called on a successful login.

    The address's own counter is left alone on purpose: one correct password does
    not vouch for the hundred wrong ones that came from the same machine.
    """
    cache.delete(_key(username, client_ip(request)))


def note_success(request, username):
    clear(request, username)
    logger.info("Login for %r from %s", username, client_ip(request))


def note_lockout(request, username):
    logger.warning(
        "Login refused (throttled) for %r from %s", username, client_ip(request)
    )
