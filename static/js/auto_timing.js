// Auto timing view. The start order (run order × start pattern) runs down the
// left as draggable tiles, grouped by run; the right shows the previous /
// current / next competitor with start, finish, run time and total time (run +
// penalties), plus a box per marshal post showing its pylon/task/stop-line
// counts — green and locked once submitted. Clicking a box opens a pop-up with
// the per-task breakdown and an Unlock button; the timekeeper can also nudge the
// total pylon/task counts. Times attach to the order automatically. State lives
// on the server; the view is nudged over the WebSocket to re-fetch.
(function () {
  "use strict";

  const URLS = window.pageData("page-urls");
  const dataEl = document.getElementById("auto-data");
  if (!URLS || !dataEl) return;

  let state = JSON.parse(dataEl.textContent);
  let openPopup = null; // { runId, post } of the marshal box whose pop-up is open
  // The list follows the current starter automatically until the operator
  // scrolls it manually; the ▲/▼ cue re-engages following.
  let following = true;
  // Optimistic single-flight state for the timekeeper's +/- so rapid clicks
  // all register: "runId:field" -> { value, queued, sending }.
  const adjustCtrl = new Map();
  // Same, for per-task pylon edits in the pop-up: "runId:post:task" -> {...}.
  const taskCtrl = new Map();
  // Browsing the tiles with the mouse wheel: a signed offset from the real current
  // (0 = the real current is centred). Purely a view — it doesn't change who is
  // current; keying a start time onto a browsed competitor is what does that.
  let browseOffset = 0;
  let lastCurrentIndex = state.current_index;
  let wheelAccum = 0;

  const orderCol = document.querySelector(".auto-order");
  const listEl = document.getElementById("auto-order-list");
  const tilesEl = document.getElementById("auto-tiles");
  const ignoredBox = document.getElementById("ignored-box");
  const emptyEl = document.getElementById("auto-empty");
  const resetBtn = document.querySelector("[data-reset]");
  const lockLabel = document.getElementById("input-lock");
  const lockCheck = document.getElementById("input-lock-check");
  const lockText = document.getElementById("input-lock-text");
  const lockNote = document.getElementById("input-lock-note");

  // Scroll-to-current affordances, floated over the top/bottom of the list.
  const scrollUp = el("button", "auto-scroll-cue auto-scroll-cue--up", gettext("▲ current"));
  const scrollDown = el("button", "auto-scroll-cue auto-scroll-cue--down", gettext("▼ current"));
  scrollUp.type = scrollDown.type = "button";
  scrollUp.hidden = scrollDown.hidden = true;
  scrollUp.addEventListener("click", returnToCurrent);
  scrollDown.addEventListener("click", returnToCurrent);
  if (orderCol) orderCol.append(scrollUp, scrollDown);
  // A manual scroll (wheel or touch) hands control to the operator.
  listEl.addEventListener("wheel", () => { following = false; }, { passive: true });
  listEl.addEventListener("touchmove", () => { following = false; }, { passive: true });

  // The mouse wheel over the tiles browses the field one competitor per notch
  // (deltaMode 1 is lines, so normalise to pixels), without changing who is
  // current — so an upcoming starter can be brought in to key a start time onto.
  tilesEl.addEventListener("wheel", (e) => {
    if (state.items.length === 0) return;
    e.preventDefault();
    wheelAccum += e.deltaMode === 1 ? e.deltaY * 33 : e.deltaY;
    while (Math.abs(wheelAccum) >= 100) {
      const dir = wheelAccum > 0 ? 1 : -1;
      setBrowse(browseOffset + dir);
      wheelAccum -= dir * 100;
    }
  }, { passive: false });

  function setBrowse(offset) {
    const n = state.items.length;
    if (n === 0) { browseOffset = 0; return; }
    const base = state.current_index >= 0 ? state.current_index : 0;
    browseOffset = Math.max(-base, Math.min(offset, n - 1 - base));  // keep centre in range
    renderTiles();
    updateFocusHighlight();
  }

  // Bring a competitor (by item index) into the centre tile — used by the wheel
  // and by clicking a start-order tile on the left.
  function focusItem(index) {
    const base = state.current_index >= 0 ? state.current_index : 0;
    setBrowse(index - base);
  }

  function updateFocusHighlight() {
    const f = focusedIndex();
    listEl.querySelectorAll(".auto-order-item").forEach((li) => {
      li.classList.toggle("auto-order-item--focused",
        browseOffset !== 0 && Number(li.dataset.index) === f);
    });
  }

  function returnToCurrent() {
    following = true;
    browseOffset = 0;
    scrollToCurrent(true);
    renderTiles();
    updateFocusHighlight();
  }

  // ---- server calls -------------------------------------------------------
  async function postJSON(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": window.csrfToken() },
      body: JSON.stringify(body || {}),
    });
    return res.json().catch(() => ({ ok: false }));
  }
  const ignore = (id, ig) => postJSON(URLS.ignore, { signal_id: id, ignored: ig });
  const pair = (id, runId, slot, slotKey) =>
    postJSON(URLS.pair, { signal_id: id, run_id: runId, slot, slot_key: slotKey });
  const setTime = (runId, slot, time, slotKey) =>
    postJSON(URLS.setTime, { run_id: runId, slot, time, slot_key: slotKey });
  const setRuntime = (runId, runTime, slotKey) =>
    postJSON(URLS.setRuntime, { run_id: runId, run_time: runTime, slot_key: slotKey });
  const setStatus = (runId, status, slotKey) =>
    postJSON(URLS.runStatus, { run_id: runId, status, slot_key: slotKey });
  const reorder = (order) => postJSON(URLS.reorder, { order });
  const resetOrder = () => postJSON(URLS.resetOrder, {});
  const adjust = (runId, field, value) => postJSON(URLS.adjust, { run_id: runId, [field]: value });
  const unlock = (runId, post) => postJSON(URLS.unlock, { run_id: runId, post });
  const lockAll = (runId) => postJSON(URLS.lockAll, { run_id: runId });
  const lockPost = (runId, post) => postJSON(URLS.lock, { run_id: runId, post });
  const taskEdit = (body) => postJSON(URLS.taskEdit, body);
  const setInputLock = (locked) => postJSON(URLS.inputLock, { locked });

  // ---- DOM helpers --------------------------------------------------------
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // ---- render -------------------------------------------------------------
  function render() {
    renderList();
    renderTiles();
    renderIgnored();
    renderLock();
    if (window.renderDeviceAlarm) window.renderDeviceAlarm(state.device_link);
    if (window.renderBarrierPhase) window.renderBarrierPhase(state.barrier);
    renderEmpty();
    // Keep the current starter in view as the field advances, unless the
    // operator has scrolled away.
    if (following) scrollToCurrent(false);
    updateScrollCues();
  }

  // With no start order, say which piece of setup is missing rather than one
  // sentence that names the run order whatever the real cause was. The server
  // decides (autotiming._empty_reason); each cause has its own line in the page.
  function renderEmpty() {
    const empty = state.items.length === 0;
    emptyEl.hidden = !empty;
    const reason = state.empty_reason || "classes";
    emptyEl.querySelectorAll("[data-empty-reason]").forEach((p) => {
      p.hidden = !empty || p.dataset.emptyReason !== reason;
    });
  }

  // The red operator lock: while on, incoming times go straight to the ignore list.
  function renderLock() {
    if (!lockCheck) return;
    const on = !!state.input_locked;
    lockCheck.checked = on;
    if (lockLabel) lockLabel.classList.toggle("input-lock--on", on);
    if (lockText) lockText.textContent = on ? gettext("Locked") : gettext("Lock");
    // The consequence, in words, in both states — this switch decides whether
    // the event's times are being kept at all.
    if (lockNote) {
      lockNote.textContent = on
        ? gettext("Incoming times go straight to Ignored — nothing is being recorded.")
        : gettext("Times are being recorded.");
      lockNote.classList.toggle("input-lock-note--on", on);
    }
  }

  function renderList() {
    const nodes = [];
    let lastGroup = null;
    state.items.forEach((item) => {
      if (item.group_index !== null && item.group_index !== lastGroup) {
        lastGroup = item.group_index;
        nodes.push(groupDivider(item.group_label));
      }
      nodes.push(orderRow(item));
    });
    listEl.replaceChildren(...nodes);
  }

  // "Run · 5, 6". The word used to come from a CSS ::before, where no catalogue
  // could reach it, so it stayed English on a German page; the muted styling is
  // still CSS's job, hence the two spans.
  function groupDivider(label) {
    const li = el("li", "auto-order-divider");
    li.append(el("span", "auto-order-divider-kind", gettext("Run") + (label ? " · " : "")));
    if (label) li.append(document.createTextNode(label));
    return li;
  }

  function orderRow(item, index) {
    const li = el("li", "auto-order-item");
    li.draggable = true;
    li.dataset.key = item.key || "";
    li.dataset.index = item.index;
    if (item.index === state.current_index) li.classList.add("auto-order-item--current");
    if (item.index === focusedIndex() && browseOffset !== 0)
      li.classList.add("auto-order-item--focused");
    if (item.finished) li.classList.add("auto-order-item--done");
    else if (item.started) li.classList.add("auto-order-item--running");
    if (item.orphan) li.classList.add("auto-order-item--orphan");
    li.append(bibChip(item, "auto-order-bib"));
    li.append(el("span", "auto-order-run", item.run_label || ""));
    const name = el("span", "auto-order-name" + (item.orphan ? " auto-order-name--orphan" : ""),
      item.name || (item.orphan ? gettext("Unattributed time") : ""));
    // The column is wide, but a long name can still run out of it — the full one
    // is a hover away rather than lost.
    if (item.name) name.title = item.name;
    li.append(name);
    // The total (run + penalties) is the meaningful figure here — unless the run
    // was closed with a state code, which is what happened *instead* of a time.
    if (item.status) {
      li.classList.add("auto-order-item--status");
      li.append(el("span", "auto-order-time auto-order-time--status", item.status.toUpperCase()));
    } else if (item.total_time) {
      li.append(el("span", "auto-order-time", item.total_time));
    }
    if (!item.orphan) li.title = gettext("Click to bring this competitor into the tiles");
    // A plain click focuses this competitor in the tiles (dragging still reorders).
    li.addEventListener("click", () => focusItem(item.index));
    li.addEventListener("dragstart", onOrderDragStart);
    li.addEventListener("dragover", onOrderDragOver);
    li.addEventListener("drop", onOrderDrop);
    li.addEventListener("dragend", onOrderDragEnd);
    return li;
  }

  // The tile index currently centred: the real current shifted by the browse
  // offset, clamped to the list. -1 only when the list is empty.
  function focusedIndex() {
    const n = state.items.length;
    if (n === 0) return -1;
    const base = state.current_index >= 0 ? state.current_index : 0;
    return Math.max(0, Math.min(base + browseOffset, n - 1));
  }

  function renderTiles() {
    const ci = state.current_index;
    const focused = focusedIndex();
    const at = (i) => (i >= 0 && i < state.items.length ? state.items[i] : null);
    // Labels track the *real* current, not the browsed centre: whatever is above
    // the current reads "Previous", the current "Current", everything below (the
    // ones a scroll-down brings up) "Next up".
    const labelFor = (i) =>
      i === ci ? gettext("Current") : ci >= 0 && i < ci ? gettext("Previous") : gettext("Next up");
    tilesEl.replaceChildren(
      tile(at(focused - 1), "prev", labelFor(focused - 1)),
      tile(focused >= 0 ? at(focused) : null, "current", labelFor(focused)),
      tile(at(focused + 1), "next", labelFor(focused + 1))
    );
    // While browsing, the centre tile isn't the running current — tone its frame
    // down so it doesn't read as one.
    tilesEl.classList.toggle("auto-tiles--browsing", browseOffset !== 0);
    positionPopup();
  }

  // Line the pop-up (and its tail) up under the marshal box that was clicked,
  // clamped to stay inside the tile.
  function positionPopup() {
    const pop = tilesEl.querySelector(".auto-popup");
    if (!pop || !openPopup) return;
    const tile = pop.closest(".auto-tile");
    const box = tile && tile.querySelector('.auto-marshal[data-post="' + openPopup.post + '"]');
    if (!box) return;
    const margin = 12;
    let left = Math.max(margin, Math.min(box.offsetLeft, tile.clientWidth - pop.offsetWidth - margin));
    pop.style.left = left + "px";
    const tail = box.offsetLeft + box.offsetWidth / 2 - left - 6;
    pop.style.setProperty("--tail-left", Math.max(10, Math.min(tail, pop.offsetWidth - 22)) + "px");
  }

  function tile(item, kind, label) {
    const div = el("div", "auto-tile auto-tile--" + kind);
    div.append(el("span", "auto-tile-label", label));
    if (!item) {
      div.classList.add("auto-tile--empty");
      div.append(el("p", "auto-tile-waiting",
        kind === "next" ? "—" : gettext("Waiting for the first start…")));
      return div;
    }

    // A run with no slot in the start order: a real time nobody owns. It used to
    // render exactly like a competitor ("#? (kein Starter)"), which is the state
    // an operator most needs help in — so it gets its own treatment and says what
    // to do about it, rather than relying on knowing that times can be dragged.
    if (item.orphan) div.classList.add("auto-tile--orphan");

    const head = el("div", "auto-tile-head");
    head.append(bibChip(item, "auto-tile-bib"));
    const id = el("div", "auto-tile-id");
    id.append(el("span", "auto-tile-name" + (item.orphan ? " auto-tile-name--orphan" : ""),
      item.orphan ? gettext("Unattributed time") : (item.name || gettext("(no starter)"))));
    const sub = [item.class_name, item.run_label].filter(Boolean).join(" · ");
    id.append(el("span", "auto-tile-sub", sub));
    head.append(id);
    div.append(head);

    if (item.orphan) div.append(orphanHelp(item));

    const times = el("div", "auto-tile-times");
    times.append(timeBlock(gettext("Start"), item.start, item, "start"));
    times.append(timeBlock(gettext("Finish"), item.finish, item, "finish"));
    times.append(runTimeFigure(item));
    times.append(item.status
      ? figure(gettext("Total"), item.status.toUpperCase(), "auto-runtime--status")
      : figure(gettext("Total"), item.total_time || "–", "auto-runtime--total"));
    div.append(times);

    // How this run ended when it didn't end in a time. Offered for an upcoming
    // competitor too (they have no run yet — the slot key makes one), because
    // "did not start" is exactly the case where nothing was ever recorded.
    if (!item.orphan && editable(item)) div.append(statusRow(item));

    // Timekeeper manual +/- on the run's total pylon / task counts.
    if (state.penalties_enabled && item.run_id) div.append(adjustRow(item));

    if (state.penalties_enabled && state.posts.length) {
      const boxes = el("div", "auto-marshals");
      // A lock-all button on the left force-submits every post at once.
      if (item.run_id) {
        const all = el("button", "auto-lock-all", "🔒");
        all.type = "button";
        all.title = gettext("Lock all posts");
        all.setAttribute("aria-label", gettext("Lock all posts"));
        all.addEventListener("click", () => lockAll(item.run_id).then(refresh));
        boxes.append(all);
      }
      // `marshals` is null when nothing has been recorded against this run — every
      // box would be the blank template `posts` already carries, so the payload
      // sends it once instead of per item (see autotiming.serialize).
      const marshals = item.marshals || state.posts;
      marshals.forEach((m) => boxes.append(marshalBox(m, item)));
      div.append(boxes);
      if (openPopup && openPopup.runId === item.run_id) {
        const box = marshals.find((m) => m.number === openPopup.post);
        if (box) div.append(popup(box, item));
        else openPopup = null;
      }
    }
    return div;
  }

  // What to do with a time that belongs to nobody: put it on the right starter,
  // or throw it away. Both were already possible; neither was said anywhere.
  /* A bib, or the space where one would be.
   *
   * The only item without one is an unattributed run — a real time no slot owns —
   * and that tile already says so in words and in flame. "#?" on it was a made-up
   * number in the field an operator reads first. The chip is kept but emptied, so
   * the tile's layout (.auto-tile-bib has a min-width) doesn't shift either.
   */
  function bibChip(item, className) {
    return item.bib == null
      ? el("span", className + " " + className + "--none", "")
      : el("span", className, "#" + item.bib);
  }

  function orphanHelp(item) {
    const box = el("div", "auto-orphan");
    // One string literal, not two concatenated: xgettext extracts what it can
    // *see*, so `gettext("a" + "b")` puts "a" in the catalog while the browser
    // looks up "ab" — a miss, and the sentence renders in English for ever. Same
    // trap as `_("…")` inside a Python f-string (see CLAUDE.md).
    box.append(el("p", "auto-orphan-text", gettext("This time has no competitor in the start order. Drag it onto the right starter's Start or Finish slot, or discard it.")));
    const ids = [item.start && item.start.id, item.finish && item.finish.id].filter(Boolean);
    if (ids.length) {
      const drop = el("button", "button button--secondary button--small", gettext("Move to Ignored"));
      drop.type = "button";
      drop.addEventListener("click", () =>
        Promise.all(ids.map((id) => ignore(id, true))).then(refresh));
      box.append(drop);
    }
    return box;
  }

  function figure(label, value, extra) {
    const wrap = el("div", "auto-time");
    wrap.append(el("span", "auto-time-label", label));
    wrap.append(el("span", "auto-runtime" + (extra ? " " + extra : ""), value));
    return wrap;
  }

  // A slot maps to a competitor we can edit when it has a run, or an upcoming
  // start-order slot we can make a run for (so a time can be keyed onto the next
  // starter before they've begun).
  const editable = (item) => Boolean(item.run_id || item.key);

  // Run time (not Total): editable by double-click, highlighted when typed in.
  function runTimeFigure(item) {
    const wrap = el("div", "auto-time");
    wrap.append(el("span", "auto-time-label", gettext("Run time")));
    const val = el("span", "auto-runtime" + (item.run_time_manual ? " auto-runtime--entered" : ""),
      item.run_time || "–");
    wrap.append(val);
    if (editable(item)) {
      wrap.title = gettext("Double-click to type a run time");
      wrap.addEventListener("dblclick", () =>
        enterRuntimeEdit(val, item, item.run_time_manual ? item.run_time : ""));
    }
    return wrap;
  }

  function timeBlock(label, sig, item, role) {
    const wrap = el("div", "auto-time");
    wrap.append(el("span", "auto-time-label", label));
    const slot = el("div", "time-slot");
    slot.dataset.role = role;
    slot.dataset.runId = item.run_id || "";
    // The slot key lets a time be dropped onto an upcoming competitor with no run
    // yet (their run is made from it), so a time can be moved onto the next starter.
    slot.dataset.slotKey = item.key || "";
    // Double-click a slot to type a time by hand (device failed, or an upcoming
    // starter); ignoring a wrong time is a drag to the Ignored list.
    if (editable(item)) {
      slot.addEventListener("dblclick", () =>
        enterTimeEdit(slot, role, item, sig ? sig.time : ""));
    }
    if (sig) {
      let cls = "time-chip";
      if (sig.entered) cls += " time-chip--entered";
      else if (sig.manual) cls += " time-chip--manual";
      const chip = el("span", cls, sig.time);
      chip.draggable = true;
      chip.dataset.signalId = sig.id;
      chip.dataset.role = role;
      chip.dataset.time = sig.time;
      chip.title = interpolate(gettext("%(kind)s · right-click to ignore · double-click to edit"),
        { kind: sig.entered ? gettext("Typed in by hand") : gettext("Drag to re-pair") }, true);
      chip.addEventListener("dragstart", (e) => onTimeDragStart(e, sig.id, role, sig.time));
      chip.addEventListener("dragend", clearTimeDrag);
      chip.addEventListener("contextmenu", (e) => ignoreOnRightClick(e, sig.id));
      slot.append(chip);
    } else {
      slot.classList.add("time-slot--empty");
      slot.append(el("span", "time-empty", "–"));
    }
    wrap.append(slot);
    return wrap;
  }

  // Swap a node for a text input pre-filled with its value; Enter/blur commits,
  // Escape cancels; the reply is a full re-fetch that redraws the tile.
  function inlineEdit(host, current, placeholder, onCommit) {
    if (host.querySelector && host.querySelector(".time-edit")) return;
    const input = el("input", "time-edit");
    input.type = "text";
    input.value = current || "";
    input.placeholder = placeholder;
    input.spellcheck = false;
    host.replaceChildren(input);
    input.focus();
    input.select();
    let done = false;
    const finish = (commit) => {
      if (done) return;
      done = true;
      if (commit) onCommit(input.value.trim());
      else refresh();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") { e.preventDefault(); finish(false); }
    });
    input.addEventListener("blur", () => finish(true));
  }

  function enterTimeEdit(slot, role, item, current) {
    inlineEdit(slot, current, "hh:mm:ss.xx",
      (value) => setTime(item.run_id, role, value, item.key).then(refresh));
  }

  function enterRuntimeEdit(host, item, current) {
    inlineEdit(host, current, "s.xx",
      (value) => setRuntime(item.run_id, value, item.key).then(refresh));
  }

  // One toggle per state code (DNF / DNC / DNS / DSQ): pressing the one already
  // set clears it. Buttons rather than a dropdown — this is a screen operated at
  // arm's length while a competitor is on course. The codes come from the server
  // (runstatus.options), so the page can't offer one the model doesn't have.
  function statusRow(item) {
    const row = el("div", "auto-status");
    row.append(el("span", "auto-status-label", gettext("Status")));
    const group = el("div", "auto-status-buttons");
    (state.status_options || []).forEach((opt) => {
      const on = item.status === opt.value;
      const btn = el("button", "auto-status-btn" + (on ? " auto-status-btn--on" : ""), opt.label);
      btn.type = "button";
      btn.title = on ? gettext("Clear this run's status") : opt.title;
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      btn.addEventListener("click", () =>
        setStatus(item.run_id, on ? "" : opt.value, item.key).then(refresh));
      group.append(btn);
    });
    row.append(group);
    return row;
  }

  function adjustRow(item) {
    const row = el("div", "auto-adjust");
    // One stepper per penalty type (Pylons / Task / Stop line). Each line carries
    // the field it edits and its non-editable base (marshal-post sum, or 0).
    item.penalties.forEach((line) => row.append(stepper(item, line)));
    return row;
  }

  function stepper(item, line) {
    // While a burst of clicks is settling, show the optimistic total.
    const key = item.run_id + ":" + line.field;
    const ctrl = adjustCtrl.get(key);
    const total = ctrl ? line.base + ctrl.value : line.total;

    const box = el("div", "auto-adjust-field");
    box.append(el("span", "auto-adjust-label", line.label));
    const controls = el("div", "auto-adjust-controls");
    const value = el("span", "auto-adjust-value", String(Math.max(0, total)));
    const minus = el("button", "pen-btn", "−");
    minus.type = "button";
    minus.addEventListener("click", () => stepAdjust(item, line, -1, value));
    const plus = el("button", "pen-btn", "+");
    plus.type = "button";
    plus.addEventListener("click", () => stepAdjust(item, line, 1, value));
    controls.append(minus, value, plus);
    box.append(controls);
    return box;
  }

  function stepAdjust(item, line, delta, valueEl) {
    const key = item.run_id + ":" + line.field;
    const ctrl = adjustCtrl.get(key) || { value: line.value, queued: null, sending: false };
    let next = ctrl.value + delta;
    if (line.base + next < 0) next = -line.base;   // the total can't go below zero
    if (next === ctrl.value) return;
    ctrl.value = next;
    ctrl.queued = next;
    adjustCtrl.set(key, ctrl);
    valueEl.textContent = String(line.base + next); // optimistic — no wait for a re-fetch
    flushAdjust(key, item.run_id, line.field);
  }

  function flushAdjust(key, runId, field) {
    const ctrl = adjustCtrl.get(key);
    if (!ctrl || ctrl.sending || ctrl.queued === null) return;
    const value = ctrl.queued;
    ctrl.queued = null;
    ctrl.sending = true;
    adjust(runId, field, value).finally(() => {
      ctrl.sending = false;
      if (ctrl.queued !== null) flushAdjust(key, runId, field);
      else { adjustCtrl.delete(key); refresh(); }  // settled → re-sync from the server
    });
  }

  function marshalBox(m, item) {
    let cls = "auto-marshal";
    if (m.submitted) cls += " auto-marshal--done";
    else if (m.entered) cls += " auto-marshal--pending";
    if (openPopup && openPopup.runId === item.run_id && openPopup.post === m.number)
      cls += " auto-marshal--open";
    const box = el("button", cls);
    box.type = "button";
    box.dataset.post = m.number;
    const head = el("span", "auto-marshal-head");
    head.append(el("span", "auto-marshal-post", "P" + m.number));
    if (m.submitted) head.append(el("span", "auto-marshal-lock", "🔒"));
    box.append(head);
    box.append(el("span", "auto-marshal-counts", boxCounts(m)));
    if (item.run_id) {
      box.addEventListener("click", () => {
        openPopup = openPopup && openPopup.runId === item.run_id && openPopup.post === m.number
          ? null
          : { runId: item.run_id, post: m.number };
        renderTiles();
      });
    } else {
      box.disabled = true;
    }
    return box;
  }

  function boxCounts(m) {
    const parts = [m.pylons + " P", m.tasks + " T"];
    if (m.stop_line) parts.push("SL");
    return parts.join(" · ");
  }

  // Speech-bubble pop-up under the tile: the post's tasks with their penalties.
  // Once the post is locked the timekeeper can +/- each task's pylons (and toggle
  // the stop line); while it's unlocked the rows are read-only and a Lock button
  // sits where Unlock would.
  function popup(box, item) {
    const editable = box.submitted;
    const pop = el("div", "auto-popup");
    pop.append(el("div", "auto-popup-title", interpolate(gettext("Post %(n)s"), { n: box.number }, true)));
    const list = el("div", "auto-popup-tasks");
    box.detail.tasks.forEach((t) => {
      const row = el("div", "auto-popup-row");
      row.append(el("span", "auto-popup-task", interpolate(gettext("Task %(n)s"), { n: t.task }, true)));
      if (editable) {
        row.append(taskStepper(item, box.number, t));
      } else {
        let mark = "—";
        if (t.task_penalty) mark = gettext("Task");
        else if (t.pylons) mark = t.pylons + " P";
        row.append(el("span", "auto-popup-mark" + (mark === "—" ? " auto-popup-mark--none" : ""), mark));
      }
      list.append(row);
    });
    if (box.detail.handles_stop_line) {
      const row = el("div", "auto-popup-row");
      row.append(el("span", "auto-popup-task", gettext("Stop line")));
      if (editable) {
        row.append(stopStepper(item, box.number, box.detail.stop_line));
      } else {
        row.append(el("span", "auto-popup-mark" + (box.detail.stop_line ? "" : " auto-popup-mark--none"),
          box.detail.stop_line ? gettext("Penalty") : "—"));
      }
      list.append(row);
    }
    pop.append(list);

    const foot = el("div", "auto-popup-foot");
    if (box.submitted) {
      foot.append(popupButton(gettext("Unlock"), () =>
        unlock(item.run_id, box.number).then(() => { openPopup = null; refresh(); })));
    } else {
      // Locking here lets the timekeeper then edit the post's per-task penalties.
      foot.append(popupButton(gettext("Lock"), () => lockPost(item.run_id, box.number).then(refresh)));
    }
    pop.append(foot);
    return pop;
  }

  function popupButton(label, onClick) {
    const btn = el("button", "button button--small", label);
    btn.type = "button";
    btn.addEventListener("click", onClick);
    return btn;
  }

  // A pylon stepper for one task, optimistic + single-flight (per run/post/task).
  function taskStepper(item, post, t) {
    const key = item.run_id + ":" + post + ":" + t.task;
    const wrap = el("div", "auto-popup-stepper");
    const start = taskCtrl.has(key) ? taskCtrl.get(key).value : t.pylons;
    const value = el("span", "auto-popup-mark", start + " P");
    const minus = el("button", "pen-btn", "−");
    minus.type = "button";
    minus.addEventListener("click", () => stepTask(key, item.run_id, post, t.task, -1, value));
    const plus = el("button", "pen-btn", "+");
    plus.type = "button";
    plus.addEventListener("click", () => stepTask(key, item.run_id, post, t.task, 1, value));
    wrap.append(minus, value, plus);
    return wrap;
  }

  function stepTask(key, runId, post, task, delta, valueEl) {
    const ctrl = taskCtrl.get(key) || { value: parseInt(valueEl.textContent, 10) || 0, queued: null, sending: false };
    const next = Math.max(0, ctrl.value + delta);
    if (next === ctrl.value) return;
    ctrl.value = next;
    ctrl.queued = { run_id: runId, post, task, pylons: next };
    taskCtrl.set(key, ctrl);
    valueEl.textContent = next + " P";
    flushTask(key);
  }

  function flushTask(key) {
    const ctrl = taskCtrl.get(key);
    if (!ctrl || ctrl.sending || ctrl.queued === null) return;
    const body = ctrl.queued;
    ctrl.queued = null;
    ctrl.sending = true;
    taskEdit(body).finally(() => {
      ctrl.sending = false;
      if (ctrl.queued !== null) flushTask(key);
      else { taskCtrl.delete(key); refresh(); }
    });
  }

  function stopStepper(item, post, on) {
    const wrap = el("div", "auto-popup-stepper");
    const label = el("span", "auto-popup-mark" + (on ? "" : " auto-popup-mark--none"), on ? gettext("Penalty") : "—");
    const toggle = el("button", "pen-btn", on ? "−" : "+");
    toggle.type = "button";
    toggle.addEventListener("click", () =>
      taskEdit({ run_id: item.run_id, post, stop_line: !on }).then(refresh));
    wrap.append(toggle, label);
    return wrap;
  }

  // Close the pop-up when clicking outside a box or the pop-up itself.
  document.addEventListener("click", (e) => {
    if (!openPopup) return;
    if (e.target.closest(".auto-marshal") || e.target.closest(".auto-popup")) return;
    openPopup = null;
    renderTiles();
  });

  // Right-click a time to ignore it. Discarding a wrong measurement is the one
  // thing an operator does in a hurry and mid-run, and the drag to the rail
  // crosses the page to get there. It is deliberately **one way**: a chip comes
  // back off the rail only by a drag or a double-click, so a right-click that
  // lands on the wrong chip can't re-pair a time onto a run — the far more
  // expensive mistake, and the one nobody would notice.
  function ignoreOnRightClick(event, signalId) {
    event.preventDefault();
    ignore(signalId, true).then(refresh);
  }

  // The rail itself is shared with Manual timing (static/js/ignored_panel.js);
  // this view only supplies the drag/restore behaviour that differs between them.
  const ignoredPanel = IgnoredPanel.create({
    box: ignoredBox,
    countEl: document.getElementById("ignored-count"),
    moreEl: document.getElementById("ignored-more"),
    onDragStart: (e, id, role, time) => onTimeDragStart(e, id, role, time),
    onDragEnd: () => clearTimeDrag(),
    onRestore: (id) => ignore(id, false).then(refresh),
  });

  function renderIgnored() {
    ignoredPanel.render(state.ignored, state.ignored_split);
  }

  // ---- scroll-to-current --------------------------------------------------
  function currentEl() {
    return listEl.querySelector(".auto-order-item--current");
  }
  function scrollToCurrent(smooth) {
    const node = currentEl();
    if (!node) return;
    // Scroll the list itself (scrollIntoView is unreliable inside a tall,
    // independently scrolling container) so the current row sits centred.
    const listBox = listEl.getBoundingClientRect();
    const itemBox = node.getBoundingClientRect();
    const delta = (itemBox.top - listBox.top) - (listEl.clientHeight - node.offsetHeight) / 2;
    listEl.scrollTo({ top: listEl.scrollTop + delta, behavior: smooth ? "smooth" : "auto" });
  }
  function updateScrollCues() {
    const node = currentEl();
    if (!node) { scrollUp.hidden = scrollDown.hidden = true; return; }
    const list = listEl.getBoundingClientRect();
    const item = node.getBoundingClientRect();
    scrollUp.hidden = item.top >= list.top;         // current is above the view
    scrollDown.hidden = item.bottom <= list.bottom; // current is below the view
  }
  listEl.addEventListener("scroll", updateScrollCues);

  // ---- reordering the start list (persisted override) ---------------------
  let dragKey = null;

  function onOrderDragStart(e) {
    dragKey = e.currentTarget.dataset.key;
    e.dataTransfer.effectAllowed = "move";
    e.currentTarget.classList.add("auto-order-item--dragging");
  }
  function onOrderDragOver(e) {
    if (dragKey === null) return;
    e.preventDefault();
    const li = e.currentTarget;
    const before = e.clientY < li.getBoundingClientRect().top + li.offsetHeight / 2;
    clearDropMarks();
    li.classList.add(before ? "auto-order-item--drop-before" : "auto-order-item--drop-after");
  }
  function onOrderDrop(e) {
    if (dragKey === null) return;
    e.preventDefault();
    const li = e.currentTarget;
    const before = e.clientY < li.getBoundingClientRect().top + li.offsetHeight / 2;
    const targetKey = li.dataset.key;
    const keys = state.items.map((it) => it.key).filter(Boolean);
    const from = keys.indexOf(dragKey);
    if (from !== -1) keys.splice(from, 1);
    let to = keys.indexOf(targetKey);
    if (to === -1) to = keys.length;
    else if (!before) to += 1;
    keys.splice(to, 0, dragKey);
    reorder(keys).then(refresh);
  }
  function onOrderDragEnd() {
    dragKey = null;
    clearDropMarks();
    listEl.querySelectorAll(".auto-order-item--dragging").forEach((n) =>
      n.classList.remove("auto-order-item--dragging"));
  }
  function clearDropMarks() {
    listEl.querySelectorAll(".auto-order-item--drop-before, .auto-order-item--drop-after")
      .forEach((n) => n.classList.remove("auto-order-item--drop-before", "auto-order-item--drop-after"));
  }

  // ---- re-pairing / ignoring times (drag) ---------------------------------
  let timeDrag = null; // { id, role, time }

  function onTimeDragStart(e, id, role, time) {
    timeDrag = { id: Number(id), role, time };
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", String(id));
  }
  function clearTimeDrag() {
    timeDrag = null;
  }

  function pairingValid(role, runId) {
    const item = state.items.find((it) => it.run_id === Number(runId));
    if (!item) return true;
    if (role === "start" && item.finish) return timeDrag.time <= item.finish.time;
    if (role === "finish" && item.start) return timeDrag.time >= item.start.time;
    return true;
  }

  function wiggle(node) {
    node.classList.remove("time-slot--reject");
    void node.offsetWidth;
    node.classList.add("time-slot--reject");
    setTimeout(() => node.classList.remove("time-slot--reject"), 450);
  }

  // A slot accepts a dragged time if the role matches and it maps to a competitor —
  // one with a run, or an upcoming one we can make a run for (drop the current's
  // time onto the next starter).
  const dropTarget = (slot) =>
    slot && timeDrag && timeDrag.role === slot.dataset.role &&
    (slot.dataset.runId || slot.dataset.slotKey);

  tilesEl.addEventListener("dragover", (e) => {
    const slot = e.target.closest(".time-slot");
    if (dropTarget(slot)) {
      e.preventDefault();
      slot.classList.add("time-slot--drop");
    }
  });
  tilesEl.addEventListener("dragleave", (e) => {
    const slot = e.target.closest(".time-slot");
    if (slot) slot.classList.remove("time-slot--drop");
  });
  tilesEl.addEventListener("drop", (e) => {
    const slot = e.target.closest(".time-slot");
    if (!dropTarget(slot)) return;
    e.preventDefault();
    slot.classList.remove("time-slot--drop");
    const runId = slot.dataset.runId;
    if (runId && !pairingValid(slot.dataset.role, runId)) {
      wiggle(slot);
      return;
    }
    pair(timeDrag.id, runId ? Number(runId) : null, slot.dataset.role, slot.dataset.slotKey)
      .then((resp) => {
        if (resp && resp.rejected) wiggle(slot);
        else refresh();
      });
  });

  ignoredBox.addEventListener("dragover", (e) => {
    if (timeDrag && !state.ignored.some((s) => s.id === timeDrag.id)) {
      e.preventDefault();
      ignoredBox.classList.add("ignored-box--drop");
    }
  });
  ignoredBox.addEventListener("dragleave", () =>
    ignoredBox.classList.remove("ignored-box--drop"));
  ignoredBox.addEventListener("drop", (e) => {
    ignoredBox.classList.remove("ignored-box--drop");
    if (timeDrag && !state.ignored.some((s) => s.id === timeDrag.id)) {
      e.preventDefault();
      ignore(timeDrag.id, true).then(refresh);
    }
  });

  if (resetBtn) resetBtn.addEventListener("click", () => resetOrder().then(refresh));

  // ---- live refresh -------------------------------------------------------
  async function refresh() {
    let data;
    try {
      data = await fetch(URLS.state).then((r) => r.json());
    } catch (err) {
      // A refresh runs on every nudge and on every reconnect — the moment the
      // network is least reliable. Losing one is fine; the next nudge (or the
      // next reconnect) re-fetches, and the connection banner shows the state.
      return;
    }
    if (!data.competition) {
      window.location.reload();
      return;
    }
    // When a new competitor becomes current (e.g. a start time was just keyed in),
    // snap the browsed view back to them.
    if (lastCurrentIndex !== null && data.current_index !== lastCurrentIndex) {
      browseOffset = 0;
    }
    lastCurrentIndex = data.current_index;
    state = data;
    render();
  }

  if (lockCheck) {
    lockCheck.addEventListener("change", () => {
      state.input_locked = lockCheck.checked;   // optimistic; the nudge confirms
      renderLock();
      setInputLock(lockCheck.checked).then(refresh);
    });
  }

  window.addEventListener("resize", updateScrollCues);
  render();
  // Socket lifecycle, connection banner and the re-fetch after an outage: see
  // live_socket.js, shared with the other live views.
  window.liveSocket({ onRefresh: refresh });
})();
