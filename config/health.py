"""``/healthz`` — something a check can be pointed at.

Until now "is it up" meant opening a page, which means logging in, which means a
person. That is fine for one laptop under the timekeeper's desk and no use at all
for the systemd unit, the reverse proxy or the phone in somebody's pocket at the
other end of the paddock.

Two decisions about what it answers:

* **It is unauthenticated**, because a check that needs a session isn't a check.
  So it must give away nothing: whether this venue is timing right now, which
  device is attached, how many people are registered and whether the backup is
  working are all *state*, and state is what the pages behind the login are for.
  This endpoint says "ok" or it says "error", and the same to everybody.
* **It touches the database.** A process that is listening but whose database has
  gone (a full disk, a locked file, an unfinished migration) is exactly the
  failure a check exists to catch, and a bare "the port answers" would report it
  as healthy. One ``SELECT 1`` — the cheapest question there is, so a monitor
  polling every few seconds costs nothing against the timing rig's own writes.
"""

import logging

from django.db import connection
from django.http import JsonResponse

logger = logging.getLogger(__name__)


def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 - any failure at all is an unhealthy server
        # Logged in full here (the log is behind the login), reported as one word.
        logger.exception("Health check failed")
        response = JsonResponse({"status": "error"}, status=503)
    else:
        response = JsonResponse({"status": "ok"})
    # A cached health check is a lie about the present.
    response["Cache-Control"] = "no-store"
    return response
