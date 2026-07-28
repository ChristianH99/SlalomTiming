"""Tag Heuer CP540 driver.

The CP540 streams line-oriented ASCII over a plain TCP socket. We only care
about its ``TN`` lines — a time for one competitor on one input:

    TN         1 M1     1:59.48100     0
    TN         2  1     2:16.39900     0
    TN    1    1 M1     3:32.93800     0   (net-time mode adds a leading column)

Each carries a running number, an input (light barrier ``1``–``4`` or a manual
trigger ``M1``–``M4`` on the same port), and the time. Everything else on the
wire (``OP``/``CL``/``RR`` status and result lines) is logged for debugging but
never turned into a timing signal.

A single background reader thread holds the connection for the whole process
(one local timing rig, matching the single-process assumption the rest of the
app makes). Start/stop it from the Timing settings page; every line it sees is
kept in a small ring buffer the settings page polls to show a live log.
"""

import re
import socket
import threading
import time
from collections import deque
from datetime import time as dt_time
from datetime import timedelta

from django.db import close_old_connections

# A time token: "m:ss.fffff", "mm:ss.fffff" or "hh:mm:ss.fffff".
_TIME_RE = re.compile(r"^\d{1,2}:\d{1,2}(?::\d{1,2})?\.\d+$")
# Input token -> (port 1–4, is a manual trigger). M-inputs share their barrier's
# port but are flagged, exactly like the simulator's M-buttons.
_INPUTS = {
    "1": (1, False), "2": (2, False), "3": (3, False), "4": (4, False),
    "M1": (1, True), "M2": (2, True), "M3": (3, True), "M4": (4, True),
}

_LOG_MAX = 400
# Longest line we will hold while waiting for its newline. A TN line is ~40
# characters; this is far past anything real, and it is here because the buffer
# below otherwise grows without limit if whatever is on the other end of the
# socket never sends one. We dial out to a configured address, but a venue LAN is
# not a trusted place to leave that assumption unstated.
_MAX_LINE = 8192

# A dropped link (knocked cable, device reboot, Wi-Fi blip) must not end the
# session: the reader keeps trying while the CP540 is the selected device, since
# every time that arrives while it is down is a time nobody can get back. Backoff
# doubles from the first delay up to the last, then stays there.
_RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0, 15.0)
# How long to nap between checks of the stop flag while waiting to retry, so
# Disconnect still takes effect immediately.
_RETRY_TICK = 0.25
# Statuses that mean the reader thread is meant to be working. Anything else is
# a link the operator has to be told about.
_LIVE_STATUSES = ("connecting", "connected", "reconnecting")


def parse_time(raw):
    """A CP540 time token -> ``datetime.time`` (wrapped at 24 h), or None. Minutes
    may exceed 59 in the two-part form, so the value is normalised through a
    timedelta rather than read positionally."""
    bits = raw.split(":")
    if len(bits) not in (2, 3):
        return None
    sec_part = bits[-1]
    whole, _, frac = sec_part.partition(".")
    if not whole.isdigit() or (frac and not frac.isdigit()):
        return None
    if not bits[-2].isdigit() or (len(bits) == 3 and not bits[0].isdigit()):
        return None
    micro = int((frac + "000000")[:6]) if frac else 0
    total = timedelta(
        hours=int(bits[0]) if len(bits) == 3 else 0,
        minutes=int(bits[-2]),
        seconds=int(whole),
    )
    secs = int(total.total_seconds()) % 86_400
    return dt_time(secs // 3600, secs % 3600 // 60, secs % 60, micro)


def parse_tn(line):
    """A raw ``TN`` line -> (running_number, port, is_manual, device_time), or
    None if it isn't a usable time line. The time token is found by shape, and
    the input sits just before it and the running number just before that — so
    both the plain and the net-time (extra leading column) forms parse the same."""
    parts = line.split()
    if len(parts) < 4 or parts[0] != "TN":
        return None
    time_idx = next((i for i, p in enumerate(parts) if _TIME_RE.match(p)), None)
    if time_idx is None or time_idx < 2:
        return None
    input_tok, running_tok = parts[time_idx - 1], parts[time_idx - 2]
    if input_tok not in _INPUTS or not running_tok.isdigit():
        return None
    device_time = parse_time(parts[time_idx])
    if device_time is None:
        return None
    port, is_manual = _INPUTS[input_tok]
    return int(running_tok), port, is_manual, device_time


class CP540Reader:
    """Owns the CP540 TCP connection on a daemon thread. Thread-safe start/stop
    and a snapshot for the settings page. One instance per process (``reader``)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._running = threading.Event()
        self._log = deque(maxlen=_LOG_MAX)
        # idle | connecting | connected | reconnecting | error | stopped
        self._status = "idle"
        self._error = ""
        self._ip = ""
        self._port = None
        self._signals = 0

    # ----- control -----

    def start(self, ip, port):
        """(Re)connect to ``ip:port``. Any existing connection is stopped first."""
        with self._lock:
            self._stop_locked()
            self._ip, self._port = ip, int(port)
            self._error = ""
            self._set_status("connecting", f"Connecting to {ip}:{port}…")
            self._running.set()
            self._thread = threading.Thread(
                target=self._worker, args=(ip, int(port)), daemon=True,
                name="cp540-reader",
            )
            self._thread.start()

    def stop(self):
        with self._lock:
            self._stop_locked()

    def _stop_locked(self):
        if self._thread and self._thread.is_alive():
            self._running.clear()
            self._thread.join(timeout=2.5)
        self._thread = None
        self._running.clear()
        if self._status in _LIVE_STATUSES:
            self._set_status("stopped")

    # ----- reporting -----

    @property
    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def snapshot(self):
        return {
            "status": self._status,
            "running": self.is_running,
            "error": self._error,
            "ip": self._ip,
            "port": self._port,
            "signals": self._signals,
            "log": list(self._log),
        }

    def _add_log(self, text, kind="other"):
        # The device's own lines are logged verbatim (columns preserved) so the
        # stream reads exactly as it arrives; our own status notes go in as-is too.
        self._log.appendleft({"text": text, "kind": kind})

    def _set_status(self, status, note="", kind="status"):
        """Move to a new connection state, log the note and — when the state
        actually changed — nudge the open timing views, so a link that goes down
        raises its alarm on the operator's screen rather than only on the
        settings page nobody is watching during a run."""
        changed = status != self._status
        self._status = status
        if note:
            self._add_log(note, kind)
        if changed:
            # Lazily imported: the reader thread must not drag the view layer in.
            from .services import notify_live
            try:
                notify_live()
            except Exception:       # a nudge is never worth killing the reader for
                pass

    # ----- worker -----

    def _worker(self, ip, port):
        """Hold the CP540 connection for as long as the reader is running,
        reconnecting with backoff whenever it drops. Only ``stop()`` ends this."""
        attempt = 0
        while self._running.is_set():
            established = self._session(ip, port)
            if not self._running.is_set():
                break
            # A link that worked and then dropped retries from the shortest delay;
            # one that never came up backs off further with each failure.
            attempt = 0 if established else attempt + 1
            delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
            self._set_status("reconnecting", f"Reconnecting to {ip}:{port} in {delay:.0f} s…", "error")
            if not self._nap(delay):
                break
            self._set_status("connecting", f"Reconnecting to {ip}:{port}…")
        close_old_connections()
        self._set_status("stopped", "Disconnected")

    def _session(self, ip, port):
        """One connection: connect, then read until it drops or we're stopped.
        Returns whether it was ever established."""
        established = False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(5.0)
                sock.connect((ip, port))
                sock.settimeout(1.0)
                established = True
                self._error = ""
                self._set_status("connected", f"Connected to {ip}:{port}")
                buffer = ""
                while self._running.is_set():
                    try:
                        data = sock.recv(1024)
                    except socket.timeout:
                        close_old_connections()
                        continue
                    if not data:
                        # Peer closed the connection.
                        if self._running.is_set():
                            self._add_log("The device closed the connection", "error")
                        break
                    buffer += data.decode(errors="ignore")
                    lines = buffer.split("\n")
                    buffer = lines.pop()  # keep any partial trailing line
                    for line in lines:
                        self._handle_line(line.rstrip("\r"))
        except OSError as exc:
            self._error = str(exc)
            self._add_log(f"Connection failed: {exc}", "error")
        finally:
            close_old_connections()
        return established

    def _nap(self, seconds):
        """Wait out a retry delay in short ticks. False if we were stopped
        meanwhile, so Disconnect doesn't have to wait for the backoff."""
        deadline = time.monotonic() + seconds
        while self._running.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(_RETRY_TICK, remaining))
        return False

    def _handle_line(self, raw):
        line = raw.strip()
        if not line:
            return
        parsed = parse_tn(line)
        if parsed is None:
            # Not a time line (status/result/other) — logged verbatim, ignored.
            self._add_log(raw, "other")
            return
        running_number, port, is_manual, device_time = parsed
        # Imported lazily so importing this module never drags in the view layer.
        from .ingest import record_signal
        try:
            signal = record_signal(running_number, port, is_manual, device_time, source="cp540")
        except Exception as exc:  # never let one bad row kill the reader
            self._add_log(f"{raw}  — not recorded: {exc}", "error")
            return
        if signal is None:
            # The DB was unavailable; the time was saved to the recovery file, not
            # dropped. Flag it so the operator knows to reconcile it later.
            self._add_log(f"{raw}  — DB busy, captured to recovery file", "error")
            return
        self._signals += 1
        self._add_log(raw, "signal")


# Process-wide reader instance.
reader = CP540Reader()


def autostart():
    """Bring the reader back up if the operator left the device connected.

    The reader thread is process state, so a restart — a crash, a laptop reboot,
    a release mid-event — used to leave the CP540 still *selected* on the
    settings page with nothing actually reading it, and no reconnect and no word
    to anybody. TimingSettings.reader_enabled is the bit that survives the
    process, and this is what acts on it.

    Called from config/asgi.py, the one entry point a server goes through (see
    singleinstance) — never from AppConfig.ready(), which would also fire for
    migrate, collectstatic and the test suite. Nothing here may stop the server
    coming up: a database that isn't migrated yet just means no autostart.
    """
    import logging

    log = logging.getLogger(__name__)
    try:
        from .models import TimingSettings

        settings = TimingSettings.load()
        if settings.device != TimingSettings.Device.CP540 or not settings.reader_enabled:
            return False
        if not settings.ip_address or not settings.port:
            return False
        reader.start(settings.ip_address, settings.port)
    except Exception:
        # An unmigrated or unreachable database, mostly. The operator can still
        # connect by hand, and the timing pages' device alarm says the link is down.
        log.warning("Could not restore the timing device connection", exc_info=True)
        return False
    log.info("Restoring the timing device connection to %s:%s",
             settings.ip_address, settings.port)
    return True


def link_state(settings=None):
    """The device link as the live timing pages show it. A device that has to
    hold a connection and hasn't got one is an alarm: every time that fires while
    it is down is gone. The simulator holds no connection, so it never alarms."""
    from django.utils.translation import gettext

    from .models import TimingSettings

    settings = settings or TimingSettings.load()
    if settings.device != TimingSettings.Device.CP540:
        return {"monitored": False, "ok": True, "status": "", "message": ""}

    snapshot = reader.snapshot()
    status = snapshot["status"]
    if status == "connected":
        message = gettext("Connected to the timing device.")
    elif status in ("connecting", "reconnecting"):
        message = gettext("No contact with the timing device — reconnecting. Times are not being recorded.")
    elif status == "idle":
        message = gettext("The timing device is not connected. Connect it on the Timing settings page.")
    else:
        message = gettext("The timing device is disconnected — times are not being recorded.")
    return {
        "monitored": True,
        "ok": status == "connected",
        "status": status,
        "message": message,
        "error": snapshot["error"],
    }
