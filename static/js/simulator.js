/* The standalone timing-device emulator (timing/simulator/).
 *
 * Was inline in its template; a Content-Security-Policy cannot tell our inline
 * script from injected inline script, so nothing on a page may be inline. Two
 * consequences visible here: the endpoint arrives as data through `json_script`,
 * and the strings go through `gettext()` (the djangojs catalog, loaded by
 * base.html) instead of `{% trans %}`.
 */

(function () {
  // Handed over as data (json_script) rather than written into an inline block:
  // nothing on a page may be inline now that the app ships a CSP.
  const URLS = window.pageData("page-urls");
  if (!URLS) return;
  const SIGNAL_URL = URLS.signal;

  const pad = (n, len = 2) => String(n).padStart(len, "0");
  const clockStr = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  const fullStr = (d) => `${clockStr(d)}.${pad(d.getMilliseconds(), 3)}`;

  // Running clock (hh:mm:ss). Ticks a few times a second so the second flips
  // promptly; the sub-second precision is captured per signal, not displayed.
  const clockEl = document.getElementById("sim-clock");
  const tick = () => { clockEl.textContent = clockStr(new Date()); };
  tick();
  setInterval(tick, 200);

  // Running number: auto-increments with each signal actually sent, resettable.
  const runnumEl = document.getElementById("sim-runnum");
  let nextNumber = 1;
  const renderRunnum = () => { runnumEl.textContent = nextNumber; };
  document.getElementById("sim-reset").addEventListener("click", () => {
    nextNumber = 1;
    renderRunnum();
  });

  const logEl = document.getElementById("sim-log");
  const logEmptyEl = document.getElementById("sim-log-empty");
  function addLog(text, kind) {
    logEmptyEl.hidden = true;
    const li = document.createElement("li");
    li.className = "sim-log-item" + (kind ? ` sim-log-item--${kind}` : "");
    li.textContent = text;
    logEl.prepend(li);
  }
  document.getElementById("sim-clear").addEventListener("click", () => {
    logEl.replaceChildren();
    logEmptyEl.hidden = false;
  });

  async function sendSignal(button) {
    const now = new Date();
    const time = fullStr(now);
    const port = Number(button.dataset.port);
    const isManual = button.dataset.manual === "1";
    const running = nextNumber;
    const label = button.dataset.label;

    button.classList.add("sim-btn--fired");
    setTimeout(() => button.classList.remove("sim-btn--fired"), 180);

    try {
      const res = await fetch(SIGNAL_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": window.csrfToken() },
        body: JSON.stringify({ running_number: running, port, is_manual: isManual, time }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || gettext("Signal rejected."));
      addLog(`#${running} · ${label} · ${time}`, isManual ? "manual" : "barrier");
      nextNumber = running + 1;
      renderRunnum();
    } catch (err) {
      addLog(gettext("#{n} · {label} · {time} — failed: {err}")
        .replace("{n}", running).replace("{label}", label)
        .replace("{time}", time).replace("{err}", err.message), "error");
    }
  }

  document.querySelectorAll(".sim-btn").forEach((button) => {
    button.addEventListener("click", () => sendSignal(button));
  });

  renderRunnum();
})();
