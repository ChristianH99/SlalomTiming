// Auto timing view. The start order (run order × start pattern) runs down the
// left as draggable tiles; the right shows the previous / current / next
// competitor with their start, finish and run time, plus a box per marshal post
// that fills as the marshal taps and turns green on submit. Times attach to the
// order automatically — the operator only reorders, ignores a wrong time, or
// drags an ignored time back onto a slot. State lives on the server; the view is
// nudged over the WebSocket to re-fetch.
(function () {
  "use strict";

  const URLS = window.AUTO_URLS;
  const CSRF = window.AUTO_CSRF;
  const dataEl = document.getElementById("auto-data");
  if (!URLS || !dataEl) return;

  let state = JSON.parse(dataEl.textContent);

  const listEl = document.getElementById("auto-order-list");
  const tilesEl = document.getElementById("auto-tiles");
  const ignoredEl = document.getElementById("auto-ignored-list");
  const emptyEl = document.getElementById("auto-empty");
  const ignoredEmptyEl = document.getElementById("auto-ignored-empty");
  const resetBtn = document.querySelector("[data-reset]");

  // ---- server calls -------------------------------------------------------
  async function postJSON(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": CSRF },
      body: JSON.stringify(body || {}),
    });
    return res.json().catch(() => ({ ok: false }));
  }
  const ignore = (id, ig) => postJSON(URLS.ignore, { signal_id: id, ignored: ig });
  const pair = (id, runId, slot) => postJSON(URLS.pair, { signal_id: id, run_id: runId, slot });
  const reorder = (order) => postJSON(URLS.reorder, { order });
  const resetOrder = () => postJSON(URLS.resetOrder, {});

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
    emptyEl.hidden = state.items.length > 0;
  }

  function renderList() {
    listEl.replaceChildren(...state.items.map(orderRow));
  }

  function orderRow(item, index) {
    const li = el("li", "auto-order-item");
    li.draggable = true;
    li.dataset.key = item.key || "";
    if (index === state.current_index) li.classList.add("auto-order-item--current");
    if (item.finished) li.classList.add("auto-order-item--done");
    else if (item.started) li.classList.add("auto-order-item--running");
    li.append(el("span", "auto-order-bib", "#" + (item.bib == null ? "?" : item.bib)));
    li.append(el("span", "auto-order-run", item.run_label || ""));
    li.append(el("span", "auto-order-name", item.name || (item.orphan ? "(extra start)" : "")));
    if (item.run_time) li.append(el("span", "auto-order-time", item.run_time));
    li.addEventListener("dragstart", onOrderDragStart);
    li.addEventListener("dragover", onOrderDragOver);
    li.addEventListener("drop", onOrderDrop);
    li.addEventListener("dragend", onOrderDragEnd);
    return li;
  }

  function renderTiles() {
    const ci = state.current_index;
    const at = (i) => (i >= 0 && i < state.items.length ? state.items[i] : null);
    tilesEl.replaceChildren(
      tile(at(ci - 1), "prev", "Previous"),
      tile(ci >= 0 ? at(ci) : null, "current", "Current"),
      tile(at(ci + 1), "next", "Next up")
    );
  }

  function tile(item, kind, label) {
    const div = el("div", "auto-tile auto-tile--" + kind);
    div.append(el("span", "auto-tile-label", label));
    if (!item) {
      div.classList.add("auto-tile--empty");
      div.append(el("p", "auto-tile-waiting",
        kind === "next" ? "—" : "Waiting for the first start…"));
      return div;
    }

    const head = el("div", "auto-tile-head");
    head.append(el("span", "auto-tile-bib", "#" + (item.bib == null ? "?" : item.bib)));
    const id = el("div", "auto-tile-id");
    id.append(el("span", "auto-tile-name", item.name || "(no starter)"));
    const sub = [item.class_name, item.run_label].filter(Boolean).join(" · ");
    id.append(el("span", "auto-tile-sub", sub));
    head.append(id);
    div.append(head);

    const times = el("div", "auto-tile-times");
    times.append(timeBlock("Start", item.start, item.run_id, "start"));
    times.append(timeBlock("Finish", item.finish, item.run_id, "finish"));
    const rt = el("div", "auto-time");
    rt.append(el("span", "auto-time-label", "Run time"));
    rt.append(el("span", "auto-runtime", item.run_time || "–"));
    times.append(rt);
    div.append(times);

    if (state.penalties_enabled && state.posts.length) {
      const boxes = el("div", "auto-marshals");
      item.marshals.forEach((m) => boxes.append(marshalBox(m)));
      div.append(boxes);
    }
    return div;
  }

  function timeBlock(label, sig, runId, role) {
    const wrap = el("div", "auto-time");
    wrap.append(el("span", "auto-time-label", label));
    const slot = el("div", "time-slot");
    slot.dataset.role = role;
    slot.dataset.runId = runId || "";
    if (sig) {
      const chip = el("span", "time-chip" + (sig.manual ? " time-chip--manual" : ""), sig.time);
      chip.draggable = true;
      chip.dataset.signalId = sig.id;
      chip.dataset.role = role;
      chip.dataset.time = sig.time;
      chip.title = "Drag to re-pair · double-click to ignore";
      chip.addEventListener("dragstart", (e) => onTimeDragStart(e, sig.id, role, sig.time));
      chip.addEventListener("dragend", clearTimeDrag);
      chip.addEventListener("dblclick", () => ignore(sig.id, true).then(refresh));
      slot.append(chip);
    } else {
      slot.classList.add("time-slot--empty");
      slot.append(el("span", "time-empty", "–"));
    }
    wrap.append(slot);
    return wrap;
  }

  function marshalBox(m) {
    let cls = "auto-marshal";
    if (m.submitted) cls += " auto-marshal--done";
    else if (m.entered) cls += " auto-marshal--pending";
    const box = el("div", cls);
    box.append(el("span", "auto-marshal-post", "P" + m.number));
    box.append(el("span", "auto-marshal-secs", (m.seconds || 0) + "s"));
    return box;
  }

  function renderIgnored() {
    ignoredEl.replaceChildren(...state.ignored.map(ignoredChip));
    ignoredEmptyEl.hidden = state.ignored.length > 0;
  }

  function ignoredChip(sig) {
    const chip = el("div", "ignored-chip" + (sig.manual ? " ignored-chip--manual" : ""));
    const tag = sig.role === "start" ? "S" : sig.role === "finish" ? "F" : "·";
    chip.append(el("span", "ignored-chip-tag", tag));
    chip.append(el("span", null, sig.time));
    chip.draggable = true;
    chip.dataset.signalId = sig.id;
    chip.dataset.role = sig.role;
    chip.dataset.time = sig.time;
    chip.title = "Drag onto a slot · double-click to restore";
    chip.addEventListener("dragstart", (e) => onTimeDragStart(e, sig.id, sig.role, sig.time));
    chip.addEventListener("dragend", clearTimeDrag);
    chip.addEventListener("dblclick", () => ignore(sig.id, false).then(refresh));
    return chip;
  }

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

  tilesEl.addEventListener("dragover", (e) => {
    const slot = e.target.closest(".time-slot");
    if (slot && timeDrag && timeDrag.role === slot.dataset.role && slot.dataset.runId) {
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
    if (!slot || !timeDrag || timeDrag.role !== slot.dataset.role || !slot.dataset.runId) return;
    e.preventDefault();
    slot.classList.remove("time-slot--drop");
    if (!pairingValid(slot.dataset.role, slot.dataset.runId)) {
      wiggle(slot);
      return;
    }
    pair(timeDrag.id, Number(slot.dataset.runId), slot.dataset.role).then((resp) => {
      if (resp && resp.rejected) wiggle(slot);
      else refresh();
    });
  });

  // Dropping a run's time onto the ignored column ignores it.
  ignoredEl.addEventListener("dragover", (e) => {
    if (timeDrag && !state.ignored.some((s) => s.id === timeDrag.id)) {
      e.preventDefault();
      ignoredEl.classList.add("auto-ignored-list--drop");
    }
  });
  ignoredEl.addEventListener("dragleave", () =>
    ignoredEl.classList.remove("auto-ignored-list--drop"));
  ignoredEl.addEventListener("drop", (e) => {
    ignoredEl.classList.remove("auto-ignored-list--drop");
    if (timeDrag && !state.ignored.some((s) => s.id === timeDrag.id)) {
      e.preventDefault();
      ignore(timeDrag.id, true).then(refresh);
    }
  });

  if (resetBtn) resetBtn.addEventListener("click", () => resetOrder().then(refresh));

  // ---- live refresh -------------------------------------------------------
  async function refresh() {
    const data = await fetch(URLS.state).then((r) => r.json());
    if (!data.competition) {
      window.location.reload();
      return;
    }
    state = data;
    render();
  }

  function connect() {
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${scheme}://${window.location.host}/ws/timing/live/`);
    ws.addEventListener("message", (event) => {
      let msg = {};
      try {
        msg = JSON.parse(event.data);
      } catch (e) {
        return;
      }
      if (msg.event === "refresh") refresh();
    });
    ws.addEventListener("close", () => setTimeout(connect, 2000));
  }

  render();
  connect();
})();
