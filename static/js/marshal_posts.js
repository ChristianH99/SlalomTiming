// Marshal Posts operator page: pick your post, then tap the task buttons to
// enter penalties for the current starter. The current starter comes from the
// Auto timing view (the competitor being timed now) over the timing WebSocket;
// every tap and the final Submit are pushed back so the timekeeper's per-post
// box fills and turns green.
(function () {
  const root = document.querySelector("[data-marshal]");
  if (!root) return;
  const config = JSON.parse(document.getElementById("marshal-config").textContent);
  // The link to the timing side; absent (e.g. in isolation) leaves the page
  // usable via the window.marshalSetStarter hook without any network calls.
  const URLS = window.MARSHAL_URLS || null;
  const CSRF = window.MARSHAL_CSRF || "";

  const select = root.querySelector("[data-post-select]");
  const confirmBtn = root.querySelector("[data-confirm]");
  const changeBtn = root.querySelector("[data-change]");
  const board = root.querySelector("[data-board]");
  const tasksWrap = root.querySelector("[data-tasks]");
  const totalEl = root.querySelector("[data-total]");
  const submitBtn = root.querySelector("[data-submit]");
  const bibEl = root.querySelector("[data-bib]");
  const nameEl = root.querySelector("[data-name]");
  const clubEl = root.querySelector("[data-club]");
  const lockEl = root.querySelector("[data-lock]");
  const toastEl = root.querySelector("[data-toast]");

  const LONG_PRESS_MS = 500;
  const storageKey = "marshalPost:" + config.competitionId;
  const postsByNumber = new Map(config.posts.map((p) => [String(p.number), p]));

  // State for the currently-selected post: task number -> {mode, pylons} plus a
  // stop-line flag. Rebuilt whenever a post is confirmed or a starter submitted.
  let state = new Map();
  let stopLine = false;
  // The current starter, or null before anyone has started. Nothing on the board
  // can be pressed or submitted while this is null — the starter arrives from the
  // timing side (or the marshalSetStarter hook).
  let starter = null;
  let confirmedPost = null;   // the post number once its selection is locked in
  let currentRunId = null;    // the timing run the current entries attach to
  let locked = false;         // submitted → no edits until a timekeeper unlocks

  // A per-device id so a post can only be held by one device at a time.
  const deviceToken = (() => {
    let t = localStorage.getItem("marshalDevice");
    if (!t) {
      t = window.crypto && crypto.randomUUID ? crypto.randomUUID()
        : String(Math.random()).slice(2) + Date.now();
      localStorage.setItem("marshalDevice", t);
    }
    return t;
  })();

  function postJSON(url, body) {
    if (!URLS) return Promise.resolve({ ok: true });
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": CSRF },
      body: JSON.stringify(body || {}),
    }).then((r) => r.json()).catch(() => ({ ok: false }));
  }

  // --- Post selection ------------------------------------------------------
  config.posts.forEach((post) => {
    const option = document.createElement("option");
    option.value = String(post.number);
    option.textContent = interpolate(gettext("Post %(n)s"), { n: post.number }, true);
    select.appendChild(option);
  });

  const saved = localStorage.getItem(storageKey);
  if (saved && postsByNumber.has(saved)) {
    select.value = saved;
    claimPost(saved).then((ok) => {
      if (ok) lockSelection();
      else { showToast(interpolate(gettext("Post %(n)s is in use on another device."), { n: saved }, true)); refreshClaims(); }
    });
  }
  refreshClaims();
  setInterval(refreshClaims, 8000);
  setInterval(() => { if (confirmedPost != null) claimPost(confirmedPost); }, 12000);  // heartbeat
  window.addEventListener("pagehide", () => releasePost(confirmedPost));

  confirmBtn.addEventListener("click", async () => {
    const number = select.value;
    if (!(await claimPost(number))) {
      showToast(interpolate(gettext("Post %(n)s is in use on another device."), { n: number }, true));
      refreshClaims();
      return;
    }
    localStorage.setItem(storageKey, number);
    lockSelection();
  });
  changeBtn.addEventListener("click", () => {
    releasePost(confirmedPost);
    select.disabled = false;
    confirmBtn.hidden = false;
    changeBtn.hidden = true;
    board.hidden = true;
    confirmedPost = null;
    currentRunId = null;
    locked = false;
    refreshClaims();
  });

  function lockSelection() {
    select.disabled = true;
    confirmBtn.hidden = true;
    changeBtn.hidden = false;
    board.hidden = false;
    confirmedPost = select.value;
    currentRunId = null;
    locked = false;
    buildBoard();
    fetchState();   // pull the current competitor for this post right away
  }

  // --- Claims (one device per post) ----------------------------------------
  async function claimPost(number) {
    if (!URLS) return true;
    const resp = await postJSON(URLS.claim, { post: Number(number), token: deviceToken });
    return !!resp.ok;
  }
  function releasePost(number) {
    if (!URLS || number == null) return;
    // keepalive so the release still goes out as the page unloads.
    fetch(URLS.release, {
      method: "POST", keepalive: true,
      headers: { "Content-Type": "application/json", "X-CSRFToken": CSRF },
      body: JSON.stringify({ post: Number(number), token: deviceToken }),
    }).catch(() => {});
  }
  async function refreshClaims() {
    if (!URLS) return;
    let taken = [];
    try {
      const resp = await fetch(URLS.claims + "?token=" + encodeURIComponent(deviceToken)).then((r) => r.json());
      taken = resp.taken || [];
    } catch (e) {
      return;
    }
    const takenSet = new Set(taken.map(String));
    Array.from(select.options).forEach((opt) => {
      const busy = takenSet.has(opt.value) && opt.value !== String(confirmedPost);
      opt.disabled = busy;
      opt.textContent = interpolate(
        busy ? gettext("Post %(n)s — in use") : gettext("Post %(n)s"),
        { n: opt.value }, true);
    });
  }

  // --- Board ---------------------------------------------------------------
  // Build the task tiles, optionally hydrated from a stored per-task detail
  // (used when a competitor's earlier state must be restored after an unlock).
  function buildBoard(detail) {
    detail = detail || {};
    const savedTasks = detail.tasks || {};
    const post = postsByNumber.get(select.value);
    state = new Map();
    stopLine = !!detail.stop_line;
    tasksWrap.innerHTML = "";
    (post.tasks || []).forEach((n) => {
      const cell = savedTasks[String(n)] || {};
      let mode = "none";
      let pylons = 0;
      if (cell.task) mode = "task";
      else if (cell.pylons) { mode = "pylon"; pylons = cell.pylons; }
      state.set(n, { mode, pylons });
      tasksWrap.appendChild(makeTaskCell(n));
    });
    if (post.stop_line) tasksWrap.appendChild(makeStopCell());
    updateTotal();
    applyEnabled();
  }

  // Each task is a big tile with a small reduce (−) button beneath it, so a
  // misjudged tap can be walked back without starting over.
  function makeTaskCell(n) {
    const cell = document.createElement("div");
    cell.className = "marshal-cell";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "marshal-task";
    btn.dataset.task = String(n);
    btn.innerHTML =
      '<span class="marshal-task-num">' + n + "</span>" +
      '<span class="marshal-task-state"></span>';
    bindPress(btn, () => tapTask(n, btn), () => longTask(n, btn));
    renderTask(n, btn);

    const reduce = document.createElement("button");
    reduce.type = "button";
    reduce.className = "marshal-reduce";
    reduce.textContent = "−";
    reduce.setAttribute("aria-label", interpolate(gettext("Reduce penalty for task %(n)s"), { n: n }, true));
    reduce.addEventListener("click", () => reduceTask(n, btn));

    cell.append(btn, reduce);
    return cell;
  }

  function makeStopCell() {
    const cell = document.createElement("div");
    cell.className = "marshal-cell";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "marshal-task marshal-task--stop";
    btn.dataset.stop = "";
    btn.innerHTML =
      '<span class="marshal-task-num">' + gettext("Stop line") + "</span>" +
      '<span class="marshal-task-state"></span>';
    bindPress(btn, () => { stopLine = !stopLine; renderStop(btn); commitChange(); }, () => {});
    renderStop(btn);
    cell.appendChild(btn);
    return cell;
  }

  // Tap = +1 pylon (bounded); long press = task penalty (toggle).
  function tapTask(n, btn) {
    const cell = state.get(n);
    if (cell.mode === "task") {
      // Leaving a task penalty: a tap starts counting pylons from one.
      cell.mode = config.maxPylons === 0 ? "none" : "pylon";
      cell.pylons = config.maxPylons === 0 ? 0 : 1;
    } else {
      const next = cell.pylons + 1;
      if (config.maxPylons !== null && next > config.maxPylons) {
        wiggle(btn);
        showToast(gettext("Max penalty per task reached"));
        return;
      }
      cell.mode = "pylon";
      cell.pylons = next;
    }
    renderTask(n, btn);
    commitChange();
  }

  function longTask(n, btn) {
    const cell = state.get(n);
    if (cell.mode === "task") {
      cell.mode = "none";
      cell.pylons = 0;
    } else {
      cell.mode = "task";
      cell.pylons = 0;
    }
    renderTask(n, btn);
    commitChange();
  }

  // Reduce = step a task's penalty back down: one pylon at a time (clearing at
  // zero), or straight off a task penalty.
  function reduceTask(n, btn) {
    const cell = state.get(n);
    if (cell.mode === "task") {
      cell.mode = "none";
    } else if (cell.mode === "pylon") {
      cell.pylons -= 1;
      if (cell.pylons <= 0) { cell.pylons = 0; cell.mode = "none"; }
    }
    renderTask(n, btn);
    commitChange();
  }

  function renderTask(n, btn) {
    const cell = state.get(n);
    const label = btn.querySelector(".marshal-task-state");
    btn.classList.toggle("marshal-task--active", cell.mode !== "none");
    btn.classList.toggle("marshal-task--task", cell.mode === "task");
    if (cell.mode === "task") label.textContent = gettext("Task");
    else if (cell.mode === "pylon") label.textContent = "×" + cell.pylons;
    else label.textContent = "—";
  }

  function renderStop(btn) {
    const label = btn.querySelector(".marshal-task-state");
    btn.classList.toggle("marshal-task--active", stopLine);
    label.textContent = stopLine ? gettext("Penalty") : "—";
  }

  // --- Total (only if the type carries penalty seconds) --------------------
  function updateTotal() {
    if (config.pylonPenalty == null) { totalEl.textContent = ""; return; }
    let seconds = 0;
    state.forEach((cell) => {
      if (cell.mode === "task") seconds += config.taskPenalty || 0;
      else if (cell.mode === "pylon") seconds += cell.pylons * config.pylonPenalty;
    });
    if (stopLine) seconds += config.stopLinePenalty || 0;
    totalEl.textContent = interpolate(gettext("Penalty: %(n)s s"), { n: seconds }, true);
  }

  // --- Starter gating ------------------------------------------------------
  // Nothing is pressable until a participant has started. The bib/name/club and
  // the starter itself will be pushed in from the timing side later; for now the
  // board sits disabled, showing the waiting placeholder.
  function setStarter(next) {
    starter = next || null;
    if (starter) {
      bibEl.textContent = starter.bib;
      nameEl.textContent = starter.name || "";
      clubEl.textContent = starter.club || "";
    } else {
      bibEl.textContent = "—";
      nameEl.textContent = gettext("Waiting for the next starter…");
      clubEl.textContent = "";
    }
    applyEnabled();
  }

  function applyEnabled() {
    // Editable only with a starter and while not locked (submitted).
    const on = !!starter && !locked;
    tasksWrap.querySelectorAll("button").forEach((btn) => { btn.disabled = !on; });
    submitBtn.disabled = !on;
    if (lockEl) lockEl.hidden = !locked;
    submitBtn.hidden = locked;
  }

  // Hook for driving the page without the timing link (tests, manual checks).
  window.marshalSetStarter = setStarter;

  // --- Submit --------------------------------------------------------------
  // Submitting locks the board here immediately (the timekeeper can unlock it);
  // no way back to edit until then.
  submitBtn.addEventListener("click", () => {
    if (!starter || locked) return;
    pushPenalty(true);
    locked = true;
    applyEnabled();
    showToast(interpolate(gettext("Penalties submitted for bib %(bib)s"), { bib: starter.bib }, true));
  });

  // --- Timing link ---------------------------------------------------------
  // Every change is pushed so the timekeeper's box tracks the marshal live; the
  // final Submit flags it done (green). Running total and the server share the
  // same aggregate.
  function commitChange() {
    updateTotal();
    pushPenalty(false);
  }

  function aggregate() {
    let pylon = 0;
    let task = 0;
    state.forEach((cell) => {
      if (cell.mode === "pylon") pylon += cell.pylons;
      else if (cell.mode === "task") task += 1;
    });
    return { pylon_count: pylon, task_count: task, stopline_count: stopLine ? 1 : 0 };
  }

  // The per-task breakdown the timekeeper's pop-up shows and the board resumes
  // from after an unlock.
  function detailObject() {
    const tasks = {};
    state.forEach((cell, n) => {
      if (cell.mode === "task") tasks[String(n)] = { task: true };
      else if (cell.mode === "pylon" && cell.pylons > 0) tasks[String(n)] = { pylons: cell.pylons };
    });
    return { tasks, stop_line: stopLine };
  }

  // Single-flight push so a burst of taps can't land out of order on the server:
  // only one request is in flight; the latest state is always what gets sent
  // last. (Fire-and-forget POSTs otherwise race — last writer wins by arrival.)
  let sending = false;
  let queued = null;
  function pushPenalty(submitted) {
    if (!URLS || currentRunId == null || confirmedPost == null) return;
    queued = Object.assign(
      { post: Number(confirmedPost), run_id: currentRunId, submitted, detail: detailObject() },
      aggregate()
    );
    flushPenalty();
  }
  function flushPenalty() {
    if (sending || !queued) return;
    const body = queued;
    queued = null;
    sending = true;
    fetch(URLS.submit, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": CSRF },
      body: JSON.stringify(body),
    })
      .catch(() => {})
      .finally(() => { sending = false; flushPenalty(); });
  }

  // Pull the current competitor for this post. A new run resets the board; the
  // same run is left alone so the marshal's in-progress taps aren't wiped.
  async function fetchState() {
    if (!URLS || confirmedPost == null) return;
    let data;
    try {
      data = await fetch(URLS.state + "?post=" + encodeURIComponent(confirmedPost))
        .then((r) => r.json());
    } catch (e) {
      return;
    }
    if (!data || data.run_id == null) {
      if (currentRunId !== null) {
        currentRunId = null;
        locked = false;
        buildBoard();
      }
      setStarter(null);
      return;
    }
    const penalty = data.penalty || {};
    if (data.run_id !== currentRunId) {
      // New competitor: hydrate the board from any stored detail and lock state.
      currentRunId = data.run_id;
      locked = !!penalty.submitted;
      buildBoard(penalty.detail);
      setStarter({ bib: data.bib, name: data.name, club: data.club });
      return;
    }
    // Same competitor: only react when the lock state flips (a timekeeper
    // unlocked it, or our submit was confirmed) — otherwise leave in-progress
    // taps untouched.
    if (!!penalty.submitted !== locked) {
      locked = !!penalty.submitted;
      buildBoard(penalty.detail);   // restore the submitted detail for editing
      applyEnabled();
    }
  }

  function connect() {
    if (!URLS) return;
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${scheme}://${window.location.host}/ws/timing/live/`);
    ws.addEventListener("message", (event) => {
      let msg = {};
      try {
        msg = JSON.parse(event.data);
      } catch (e) {
        return;
      }
      if (msg.event === "refresh") fetchState();
    });
    ws.addEventListener("close", () => setTimeout(connect, 2000));
  }
  connect();

  // --- Helpers -------------------------------------------------------------
  // Distinguish a tap from a long press with one timer; suppress the tap that
  // would otherwise follow a long press, and cancel if the finger leaves.
  function bindPress(btn, onTap, onLong) {
    let timer = null;
    let longFired = false;
    let active = false;
    const clear = () => { if (timer) { clearTimeout(timer); timer = null; } };
    btn.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      active = true;
      longFired = false;
      timer = setTimeout(() => { longFired = true; timer = null; onLong(); }, LONG_PRESS_MS);
    });
    btn.addEventListener("pointerup", (event) => {
      if (!active) return;
      event.preventDefault();
      active = false;
      clear();
      if (!longFired) onTap();
    });
    const abort = () => { active = false; clear(); };
    btn.addEventListener("pointercancel", abort);
    btn.addEventListener("pointerleave", abort);
    btn.addEventListener("contextmenu", (event) => event.preventDefault());
  }

  function wiggle(btn) {
    btn.classList.remove("marshal-task--wiggle");
    void btn.offsetWidth;   // restart the animation
    btn.classList.add("marshal-task--wiggle");
  }

  let toastTimer = null;
  function showToast(message) {
    toastEl.textContent = message;
    toastEl.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.hidden = true; }, 2200);
  }
})();
