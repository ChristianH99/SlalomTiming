// The live WebSocket, shared by every view that follows the event as it runs:
// Manual timing, Auto timing, Marshal Posts and the Dashboard.
//
// Each of those used to open its own socket with the same six lines, and those
// six lines had two holes an operator pays for on race day:
//
//   * nothing on the page said whether the socket was up, so a dropped
//     connection left a frozen screen that looked exactly like a live one; and
//   * a reconnect re-subscribed but never re-fetched, so every signal that
//     arrived during the outage stayed invisible until the *next* one happened
//     to arrive — at the end of a run group, possibly never.
//
// So the lifecycle lives here once: reconnect with backoff, re-fetch on every
// reconnect, and render the connection state into the shared banner
// (templates/timing/_live_connection.html) wherever the page includes one.
//
// A socket can also die without a close event — a phone that walks out of Wi-Fi
// range gets no TCP FIN — which is the frozen screen again, this time with a
// green light on it. So the client pings and expects a pong; silence means the
// link is gone whatever the readyState claims.
(function () {
  // Backoff between reconnect attempts. Starts fast: most drops are a blip and
  // the operator is mid-run.
  const RETRY_MS = [1000, 2000, 4000, 8000, 15000];
  const PING_EVERY_MS = 20000;
  const PONG_WAIT_MS = 10000;

  function bannerEl() {
    return document.querySelector("[data-live-connection]");
  }

  // state: "online" | "offline"
  function renderBanner(state) {
    const box = bannerEl();
    if (!box) return;
    const text = box.querySelector("[data-live-connection-text]");
    const online = state === "online";
    box.classList.toggle("live-conn--online", online);
    box.classList.toggle("live-conn--offline", !online);
    if (text) {
      text.textContent = online
        ? gettext("Live")
        : gettext("Connection lost — this screen is not updating. Reconnecting…");
    }
    box.hidden = false;
  }

  // The active competition is one global flag: someone pressing "Set as current"
  // moves every screen in the venue at once. The data on this page is about to
  // become another event's, so say so rather than let it re-render silently.
  function competitionChanged(name) {
    let bar = document.getElementById("live-event-changed");
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "live-event-changed";
      bar.className = "live-notice";
      bar.setAttribute("role", "alert");
      const text = document.createElement("span");
      text.className = "live-notice-text";
      const reload = document.createElement("button");
      reload.type = "button";
      reload.className = "live-notice-action";
      reload.textContent = gettext("Reload");
      reload.addEventListener("click", () => window.location.reload());
      bar.append(text, reload);
      document.body.prepend(bar);
    }
    bar.querySelector(".live-notice-text").textContent = name
      ? interpolate(gettext("The current event was changed to “%(name)s”. This screen now shows that event."), { name: name }, true)
      : gettext("The current event was changed. This screen now shows another event.");
  }

  window.liveSocket = function (options) {
    const opts = options || {};
    const onRefresh = opts.onRefresh || function () {};
    const onState = opts.onState || null;
    // Called when the active competition changed underneath this page.
    const onCompetition = opts.onCompetition || competitionChanged;

    let ws = null;
    let attempt = 0;
    let everOpened = false;
    let retryTimer = null;
    let pingTimer = null;
    let pongTimer = null;

    function setState(state) {
      renderBanner(state);
      if (onState) onState(state);
    }

    function clearTimers() {
      if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
      if (pongTimer) { clearTimeout(pongTimer); pongTimer = null; }
    }

    // Treat the socket as dead and start over. Called both from `close` and from
    // a ping that went unanswered.
    function drop() {
      clearTimers();
      if (ws) {
        const dead = ws;
        ws = null;
        dead.onclose = null;
        try { dead.close(); } catch (e) { /* already gone */ }
      }
      setState("offline");
      scheduleRetry();
    }

    function scheduleRetry() {
      if (retryTimer) return;
      const delay = RETRY_MS[Math.min(attempt, RETRY_MS.length - 1)];
      attempt += 1;
      retryTimer = setTimeout(() => { retryTimer = null; connect(); }, delay);
    }

    function startHeartbeat() {
      clearTimers();
      pingTimer = setInterval(() => {
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
        try {
          ws.send(JSON.stringify({ action: "ping" }));
        } catch (e) {
          drop();
          return;
        }
        // No pong in time = a link that is gone without saying so.
        if (!pongTimer) pongTimer = setTimeout(drop, PONG_WAIT_MS);
      }, PING_EVERY_MS);
    }

    function connect() {
      if (ws) return;
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      let socket;
      try {
        socket = new WebSocket(`${scheme}://${window.location.host}/ws/timing/live/`);
      } catch (e) {
        setState("offline");
        scheduleRetry();
        return;
      }
      ws = socket;

      socket.addEventListener("open", () => {
        attempt = 0;
        setState("online");
        startHeartbeat();
        // The whole point of OPS-4: anything that happened while we were away is
        // sitting on the server, and nothing else will come and tell us about it.
        if (everOpened) onRefresh();
        everOpened = true;
      });

      socket.addEventListener("message", (event) => {
        let msg = {};
        try {
          msg = JSON.parse(event.data);
        } catch (e) {
          return;
        }
        if (msg.event === "pong") {
          if (pongTimer) { clearTimeout(pongTimer); pongTimer = null; }
          return;
        }
        if (msg.event === "refresh") onRefresh();
        if (msg.event === "competition") {
          onCompetition(msg.name || "");
          onRefresh();
        }
      });

      socket.addEventListener("close", () => {
        if (ws !== socket) return;   // already replaced by drop()
        ws = null;
        clearTimers();
        setState("offline");
        scheduleRetry();
      });
    }

    // A phone coming back onto the network, or a tab brought back to the front,
    // shouldn't have to wait out the backoff.
    function wakeUp() {
      if (ws) return;
      if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
      attempt = 0;
      connect();
    }
    window.addEventListener("online", wakeUp);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) wakeUp();
    });

    setState("offline");
    connect();
    return { connect, wakeUp };
  };
})();
