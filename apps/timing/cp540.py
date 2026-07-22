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
        self._status = "idle"          # idle | connecting | connected | error | stopped
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
            self._status = "connecting"
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
        if self._status in ("connecting", "connected"):
            self._status = "stopped"

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

    # ----- worker -----

    def _worker(self, ip, port):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(5.0)
                sock.connect((ip, port))
                sock.settimeout(1.0)
                self._status = "connected"
                self._add_log(f"Connected to {ip}:{port}", "status")
                buffer = ""
                while self._running.is_set():
                    try:
                        data = sock.recv(1024)
                    except socket.timeout:
                        close_old_connections()
                        continue
                    if not data:
                        # Peer closed the connection.
                        break
                    buffer += data.decode(errors="ignore")
                    lines = buffer.split("\n")
                    buffer = lines.pop()  # keep any partial trailing line
                    for line in lines:
                        self._handle_line(line.rstrip("\r"))
        except OSError as exc:
            self._status = "error"
            self._error = str(exc)
            self._add_log(f"Connection failed: {exc}", "error")
        finally:
            close_old_connections()
            if self._status not in ("error",):
                self._status = "stopped"
                self._add_log("Disconnected", "status")

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
