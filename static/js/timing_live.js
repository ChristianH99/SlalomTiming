// Live timing view. Renders runs (a start time paired with a finish time) newest
// first, lets the operator enter bib/class/run/penalties, double-click a time to
// ignore it, and drag a time into a run's start/finish slot to pair it. State
// lives on the server; edits POST and the view is nudged over a WebSocket to
// re-fetch when signals or assignments change.
(function () {
  "use strict";

  const URLS = window.TIMING_URLS;
  const CSRF = window.TIMING_CSRF;
  const dataEl = document.getElementById("timing-data");
  if (!URLS || !dataEl) return;

  let state = JSON.parse(dataEl.textContent);

  const rowsEl = document.getElementById("timing-rows");
  const emptyEl = document.getElementById("timing-empty");
  const ignoredList = document.getElementById("timing-ignored-list");
  const ignoredZone = document.getElementById("timing-ignored-zone");

  // ---- server calls -------------------------------------------------------
  async function postJSON(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": CSRF },
      body: JSON.stringify(body),
    });
    return res.json().catch(() => ({ ok: false }));
  }
  const updateRun = (body) => postJSON(URLS.run, body);
  const setIgnored = (signalId, ignored) => postJSON(URLS.ignore, { signal_id: signalId, ignored });
  const pair = (signalId, runId, slot) => postJSON(URLS.pair, { signal_id: signalId, run_id: runId, slot });

  // ---- small DOM helpers --------------------------------------------------
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function cell(className, child) {
    const td = el("td", className);
    if (child) td.append(child);
    return td;
  }

  // ---- rendering ----------------------------------------------------------
  function render() {
    rowsEl.replaceChildren(...state.rows.map(renderRow));
    emptyEl.hidden = state.rows.length > 0;
    renderIgnored();
  }

  function renderRow(row) {
    const run = row.run;
    const tr = el("tr", "timing-row");
    tr.dataset.runId = run.id;
    if (run.over_max) tr.classList.add("timing-row--over");

    tr.append(cell("tt-time", timeSlot(row.start, "start", run.id)));
    tr.append(cell("tt-time", timeSlot(row.finish, "finish", run.id)));
    tr.append(cell("tt-run", el("span", "run-time", row.run_time || "–")));
    tr.append(cell("tt-bib", bibField(run)));
    tr.append(cell("tt-class", classField(run)));
    tr.append(cell("tt-run-sel", runField(run)));
    if (state.penalties_enabled) {
      tr.append(cell("tt-pen", penaltyBox(run, "pylon_count")));
      tr.append(cell("tt-pen", penaltyBox(run, "task_count")));
      tr.append(cell("tt-pen", penaltyBox(run, "stopline_count")));
      tr.append(cell("tt-pen-total", el("span", "pen-total", secs(run.penalty))));
    }
    tr.append(cell("tt-total", el("span", "run-total", run.total || "–")));
    return tr;
  }

  function timeSlot(sig, role, runId) {
    const wrap = el("div", "time-slot");
    wrap.dataset.role = role;
    wrap.dataset.runId = runId;
    if (!sig) {
      wrap.classList.add("time-slot--empty");
      wrap.append(el("span", "time-empty", "–"));
      return wrap;
    }
    wrap.dataset.time = sig.time;
    const chip = el("span", "time-chip", sig.time);
    chip.draggable = true;
    chip.dataset.signalId = sig.id;
    chip.dataset.role = role;
    chip.dataset.time = sig.time;
    chip.title = "Drag to pair · double-click to ignore";
    chip.addEventListener("dragstart", (e) => onDragStart(e, sig.id, role, sig.time));
    chip.addEventListener("dragend", clearDrag);
    chip.addEventListener("dblclick", () => setIgnored(sig.id, true).then(refresh));
    wrap.append(chip);
    return wrap;
  }

  function bibField(run) {
    const wrap = el("div", "bib-field");
    const input = el("input", "bib-input");
    input.type = "number";
    input.min = "1";
    input.value = run.bib_number == null ? "" : run.bib_number;
    input.dataset.rowKey = run.id;
    input.dataset.field = "bib";
    input.addEventListener("change", () =>
      updateRun({ run_id: run.id, bib_number: input.value }).then(applyRow)
    );
    // Name line is always present so the row height never changes when it fills in.
    wrap.append(input, el("span", "bib-name", run.name || ""));
    return wrap;
  }

  function classField(run) {
    if (state.multi_class && run.class_options.length) {
      const select = el("select", "class-select");
      select.dataset.rowKey = run.id;
      select.dataset.field = "class";
      run.class_options.forEach((opt) => {
        const o = el("option", null, opt.label);
        o.value = opt.value;
        if (String(opt.value) === String(run.class_id)) o.selected = true;
        select.append(o);
      });
      select.addEventListener("change", () =>
        updateRun({ run_id: run.id, class_id: select.value }).then(applyRow)
      );
      return select;
    }
    return el("span", "class-static", run.class_name || "–");
  }

  function runField(run) {
    const select = el("select", "run-select");
    select.dataset.rowKey = run.id;
    select.dataset.field = "run";
    const blank = el("option", null, "–");
    blank.value = "";
    select.append(blank);
    run.run_options.forEach((opt) => {
      const o = el("option", null, opt.label);
      o.value = opt.value;
      if (opt.value === run.run_value) o.selected = true;
      select.append(o);
    });
    select.addEventListener("change", () =>
      updateRun({ run_id: run.id, run_value: select.value }).then(applyRow)
    );
    return select;
  }

  function penaltyBox(run, field) {
    const input = el("input", "penalty-input");
    input.type = "number";
    input.min = "0";
    input.value = run[field];
    input.dataset.rowKey = run.id;
    input.dataset.field = field;
    input.addEventListener("change", () => {
      const v = Math.max(0, parseInt(input.value, 10) || 0);
      input.value = v;
      updateRun({ run_id: run.id, [field]: v }).then(applyRow);
    });
    return input;
  }

  function renderIgnored() {
    ignoredList.replaceChildren(
      ...state.ignored.map((sig) => {
        const li = el("li", "ignored-item");
        li.draggable = true;
        li.dataset.signalId = sig.id;
        li.dataset.role = sig.role;
        li.dataset.time = sig.time;
        li.addEventListener("dragstart", (e) => onDragStart(e, sig.id, sig.role, sig.time));
        li.addEventListener("dragend", clearDrag);
        li.append(el("span", "ignored-role", sig.role || "?"), el("span", "ignored-time", sig.time));
        const use = el("button", "link-button", "use");
        use.type = "button";
        use.addEventListener("click", () => setIgnored(sig.id, false).then(refresh));
        li.append(use);
        return li;
      })
    );
  }

  const secs = (n) => (n ? `${n}s` : "0s");

  // ---- apply a single updated row (edit reply) ----------------------------
  function applyRow(resp) {
    if (!resp || !resp.ok || !resp.row) return;
    const i = state.rows.findIndex((r) => r.id === resp.row.id);
    if (i !== -1) state.rows[i] = resp.row;
    const tr = rowsEl.querySelector(`tr[data-run-id="${resp.row.id}"]`);
    if (tr) tr.replaceWith(renderRow(resp.row));
  }

  // ---- drag and drop (pairing) -------------------------------------------
  let dragged = null; // { id, role, time }

  function onDragStart(e, id, role, time) {
    dragged = { id: Number(id), role, time };
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", String(id));
  }
  function clearDrag() {
    dragged = null;
  }

  // A start can only go after a finish it shares a row with; a finish only before.
  function pairingValid(slot, targetRunId) {
    const row = state.rows.find((r) => r.id === Number(targetRunId));
    if (!row) return true;
    if (slot === "start" && row.finish) return dragged.time <= row.finish.time;
    if (slot === "finish" && row.start) return dragged.time >= row.start.time;
    return true;
  }

  function wiggle(node) {
    node.classList.remove("time-slot--reject");
    void node.offsetWidth; // restart the animation
    node.classList.add("time-slot--reject");
    setTimeout(() => node.classList.remove("time-slot--reject"), 450);
  }

  rowsEl.addEventListener("dragover", (e) => {
    const slot = e.target.closest(".time-slot");
    // Only a matching role may drop here (start into start, finish into finish).
    if (slot && dragged && dragged.role === slot.dataset.role) {
      e.preventDefault();
      slot.classList.add("time-slot--drop");
    }
  });
  rowsEl.addEventListener("dragleave", (e) => {
    const slot = e.target.closest(".time-slot");
    if (slot) slot.classList.remove("time-slot--drop");
  });
  rowsEl.addEventListener("drop", (e) => {
    const slot = e.target.closest(".time-slot");
    if (!slot || !dragged || dragged.role !== slot.dataset.role) return;
    e.preventDefault();
    slot.classList.remove("time-slot--drop");
    if (!pairingValid(slot.dataset.role, slot.dataset.runId)) {
      wiggle(slot);
      return;
    }
    pair(dragged.id, Number(slot.dataset.runId), slot.dataset.role).then((resp) => {
      if (resp && resp.rejected) wiggle(slot);
      else refresh();
    });
  });

  // ---- live refresh (WebSocket nudge) -------------------------------------
  async function refresh() {
    // Preserve the field being edited so a streamed-in time doesn't drop focus.
    const active = document.activeElement;
    let key = null;
    if (active && active.dataset && active.dataset.rowKey !== undefined) {
      key = { row: active.dataset.rowKey, field: active.dataset.field, value: active.value, caret: active.selectionStart };
    }
    const data = await fetch(URLS.arrangement).then((r) => r.json());
    if (!data.competition) {
      window.location.reload();
      return;
    }
    state = data;
    render();
    if (key) {
      const sel = rowsEl.querySelector(`[data-row-key="${key.row}"][data-field="${key.field}"]`);
      if (sel) {
        sel.focus();
        if (key.value !== undefined && sel.value !== key.value) sel.value = key.value;
        try {
          sel.setSelectionRange(key.caret, key.caret);
        } catch (err) {
          /* number inputs don't support selection range */
        }
      }
    }
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
