/* Timing > Settings: showing the block that belongs to the selected device, and
 * polling the CP540's live status and raw-line log.
 *
 * Was inline in its template. Nothing on a page may be inline now that the app
 * ships a Content-Security-Policy, so the endpoint arrives as data, the strings
 * go through `gettext()` (the djangojs catalog), and the form's controls are
 * found by `data-` marker rather than by the id Django rendered for them.
 */

(function () {
  // The form's controls are found by marker, not by the id Django rendered for
  // them: a file can't be handed a template variable, and the coupling is better
  // this way — the script no longer knows how the form names its fields.
  const select = document.querySelector("[data-device-select]");
  if (!select) return;
  const groups = [...document.querySelectorAll("[data-device-group]")];
  const starts = [...document.querySelectorAll("[data-start]")];

  function apply() {
    const device = select.value;
    groups.forEach((el) => { el.hidden = el.dataset.deviceGroup !== device; });
    starts.forEach((el) => { el.hidden = el.dataset.start !== device; });
    pollIfCp540();
  }
  select.addEventListener("change", apply);

  // ----- CP540 live status/log poll -----
  const STATUS_URL = (window.pageData("page-urls") || {}).status;
  const statusEl = document.getElementById("cp540-status");
  const logEl = document.getElementById("cp540-log");
  const logEmptyEl = document.getElementById("cp540-log-empty");
  const connectBtn = document.getElementById("cp540-connect");
  const disconnectBtn = document.getElementById("cp540-disconnect");
  const ipInput = document.querySelector("[data-device-ip]");
  const portInput = document.querySelector("[data-device-port]");
  const lockedHint = document.getElementById("cp540-locked-hint");
  const STATUS_TEXT = {
    idle: gettext("Idle"), connecting: gettext("Connecting…"),
    connected: gettext("Connected"), reconnecting: gettext("Reconnecting…"),
    error: gettext("Connection error"), stopped: gettext("Disconnected"),
  };
  let timer = null;

  function applyLink(running) {
    // Connect makes no sense while connected; Disconnect none while idle.
    if (connectBtn) connectBtn.disabled = running;
    if (disconnectBtn) disconnectBtn.disabled = !running;
    // The address/port can't change on a live connection.
    [ipInput, portInput].forEach((el) => {
      if (el) el.readOnly = running;
    });
    if (lockedHint) lockedHint.hidden = !running;
  }

  async function refresh() {
    try {
      const res = await fetch(STATUS_URL, { headers: { "Accept": "application/json" } });
      const data = await res.json();
      const state = data.status || "idle";
      statusEl.dataset.state = data.error ? "error" : state;
      statusEl.textContent = (STATUS_TEXT[state] || state)
        + (data.error ? ` — ${data.error}` : "")
        + (data.running && data.signals
            ? " · " + (data.signals === 1
                ? gettext("{n} signal").replace("{n}", data.signals)
                : gettext("{n} signals").replace("{n}", data.signals))
            : "");
      applyLink(!!data.running);
      renderLog(data.log || []);
    } catch (err) { /* transient; try again next tick */ }
  }

  function renderLog(lines) {
    logEmptyEl.hidden = lines.length > 0;
    logEl.replaceChildren(...lines.map((line) => {
      const li = document.createElement("li");
      li.className = "device-log-line device-log-line--" + (line.kind || "other");
      li.textContent = line.text || "";
      return li;
    }));
  }

  function pollIfCp540() {
    const on = select.value === "cp540";
    if (on && timer === null) {
      refresh();
      timer = setInterval(refresh, 1000);
    } else if (!on && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }

  apply();
})();
