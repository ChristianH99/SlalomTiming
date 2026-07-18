// Marshal Posts operator page: pick your post, then tap the task buttons to
// enter penalties for the current starter. Front-end only for now — Submit is a
// local stub; wiring the entries back into the system is a later feature.
(function () {
  const root = document.querySelector("[data-marshal]");
  if (!root) return;
  const config = JSON.parse(document.getElementById("marshal-config").textContent);

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
  const toastEl = root.querySelector("[data-toast]");

  const LONG_PRESS_MS = 500;
  const storageKey = "marshalPost:" + config.competitionId;
  const postsByNumber = new Map(config.posts.map((p) => [String(p.number), p]));

  // State for the currently-selected post: task number -> {mode, pylons} plus a
  // stop-line flag. Rebuilt whenever a post is confirmed or a starter submitted.
  let state = new Map();
  let stopLine = false;
  // The current starter, or null before anyone has started. Nothing on the board
  // can be pressed or submitted while this is null — the bib arrives later, from
  // the timing side, via marshalSetStarter().
  let starter = null;

  // --- Post selection ------------------------------------------------------
  config.posts.forEach((post) => {
    const option = document.createElement("option");
    option.value = String(post.number);
    option.textContent = "Post " + post.number;
    select.appendChild(option);
  });

  const saved = localStorage.getItem(storageKey);
  if (saved && postsByNumber.has(saved)) {
    select.value = saved;
    lockSelection();
  }

  confirmBtn.addEventListener("click", () => {
    localStorage.setItem(storageKey, select.value);
    lockSelection();
  });
  changeBtn.addEventListener("click", () => {
    select.disabled = false;
    confirmBtn.hidden = false;
    changeBtn.hidden = true;
    board.hidden = true;
  });

  function lockSelection() {
    select.disabled = true;
    confirmBtn.hidden = true;
    changeBtn.hidden = false;
    board.hidden = false;
    buildBoard();
  }

  // --- Board ---------------------------------------------------------------
  function buildBoard() {
    const post = postsByNumber.get(select.value);
    state = new Map();
    stopLine = false;
    tasksWrap.innerHTML = "";
    (post.tasks || []).forEach((n) => {
      state.set(n, { mode: "none", pylons: 0 });
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
    reduce.setAttribute("aria-label", "Reduce penalty for task " + n);
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
      '<span class="marshal-task-num">Stop line</span>' +
      '<span class="marshal-task-state"></span>';
    bindPress(btn, () => { stopLine = !stopLine; renderStop(btn); updateTotal(); }, () => {});
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
        showToast("Max penalty per task reached");
        return;
      }
      cell.mode = "pylon";
      cell.pylons = next;
    }
    renderTask(n, btn);
    updateTotal();
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
    updateTotal();
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
    updateTotal();
  }

  function renderTask(n, btn) {
    const cell = state.get(n);
    const label = btn.querySelector(".marshal-task-state");
    btn.classList.toggle("marshal-task--active", cell.mode !== "none");
    btn.classList.toggle("marshal-task--task", cell.mode === "task");
    if (cell.mode === "task") label.textContent = "Task";
    else if (cell.mode === "pylon") label.textContent = "×" + cell.pylons;
    else label.textContent = "—";
  }

  function renderStop(btn) {
    const label = btn.querySelector(".marshal-task-state");
    btn.classList.toggle("marshal-task--active", stopLine);
    label.textContent = stopLine ? "Penalty" : "—";
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
    totalEl.textContent = "Penalty: " + seconds + " s";
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
      nameEl.textContent = "Waiting for the next starter…";
      clubEl.textContent = "";
    }
    applyEnabled();
  }

  function applyEnabled() {
    const on = !!starter;
    tasksWrap.querySelectorAll("button").forEach((btn) => { btn.disabled = !on; });
    submitBtn.disabled = !on;
  }

  // Hook for the (later) timing integration to announce who's on course.
  window.marshalSetStarter = setStarter;

  // --- Submit (stub) -------------------------------------------------------
  submitBtn.addEventListener("click", () => {
    if (!starter) return;
    showToast("Penalties submitted for bib " + starter.bib);
    setStarter(null);
    buildBoard();
  });

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
