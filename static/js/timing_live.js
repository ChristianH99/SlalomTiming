// Live timing view. Renders runs (a start time paired with a finish time) newest
// first, with pre-enterable placeholder rows at the top; lets the operator enter
// bib/class/run/penalties, double-click a time to ignore it (ignored starts and
// finishes each get their own rail column on the right, beside where they fall),
// and drag a time onto a run's slot to pair it. State lives on the server; edits
// POST and the view is nudged over a WebSocket to re-fetch.
(function () {
  "use strict";

  const URLS = window.TIMING_URLS;
  const CSRF = window.TIMING_CSRF;
  const dataEl = document.getElementById("timing-data");
  if (!URLS || !dataEl) return;

  let state = JSON.parse(dataEl.textContent);

  const rowsEl = document.getElementById("timing-rows");
  const emptyEl = document.getElementById("timing-empty");
  const rail = document.getElementById("ignored-rail");

  // ---- server calls -------------------------------------------------------
  async function postJSON(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": CSRF },
      body: JSON.stringify(body || {}),
    });
    return res.json().catch(() => ({ ok: false }));
  }
  const updateRun = (body) => postJSON(URLS.run, body);
  const addRun = () => postJSON(URLS.runAdd);
  const deleteRun = (runId) => postJSON(URLS.runDelete, { run_id: runId });
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
  const colspan = () => (state.penalties_enabled ? 11 : 7);

  // ---- rendering ----------------------------------------------------------
  function render() {
    rowsEl.replaceChildren(addStrip(), ...state.rows.map(renderRow));
    emptyEl.hidden = state.rows.length > 0;
    renderIgnored();
  }

  // A thin strip below the header; hover reveals a + to add a placeholder row.
  function addStrip() {
    const tr = el("tr", "tt-add-strip");
    const td = el("td");
    td.colSpan = colspan();
    const btn = el("button", "tt-add-btn", "+");
    btn.type = "button";
    btn.title = "Add a row for an upcoming starter";
    btn.addEventListener("click", () => addRun().then(refresh));
    td.append(btn);
    tr.append(td);
    return tr;
  }

  function renderRow(row) {
    const run = row.run;
    const tr = el("tr", "timing-row");
    tr.dataset.runId = run.id;
    if (row.placeholder) tr.classList.add("timing-row--placeholder");
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
    const totalCell = cell("tt-total", el("span", "run-total", run.total || "–"));
    // An empty placeholder can be removed.
    if (row.placeholder) {
      const del = el("button", "row-delete", "×");
      del.type = "button";
      del.tabIndex = -1;
      del.title = "Remove this row";
      del.addEventListener("click", () => deleteRun(run.id).then(refresh));
      totalCell.append(del);
    }
    tr.append(totalCell);
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
    const chip = el("span", "time-chip" + (sig.manual ? " time-chip--manual" : ""), sig.time);
    chip.draggable = true;
    chip.dataset.signalId = sig.id;
    chip.dataset.role = role;
    chip.dataset.time = sig.time;
    chip.title = (sig.manual ? "Manual" : "Light barrier") + " · drag to pair · double-click to ignore";
    chip.addEventListener("dragstart", (e) => onDragStart(e, sig.id, role, sig.time));
    chip.addEventListener("dragend", clearDrag);
    chip.addEventListener("dblclick", () => setIgnored(sig.id, true).then(refresh));
    wrap.append(chip);
    return wrap;
  }

  function bibField(run) {
    const wrap = el("div", "bib-field");
    const input = el("input", "bib-input" + (run.bib_unknown ? " bib-input--unknown" : ""));
    input.type = "number";
    input.min = "1";
    input.value = run.bib_number == null ? "" : run.bib_number;
    input.dataset.rowKey = run.id;
    input.dataset.field = "bib";
    if (run.bib_unknown) input.title = "No starter with this bib is registered (kept anyway).";
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
      if (opt.disabled && opt.value !== run.run_value) o.disabled = true;
      if (opt.value === run.run_value) o.selected = true;
      select.append(o);
    });
    select.addEventListener("change", () =>
      updateRun({ run_id: run.id, run_value: select.value }).then(applyRow)
    );
    return select;
  }

  function penaltyBox(run, field) {
    const wrap = el("div", "pen-stepper");
    const input = el("input", "penalty-input");
    input.type = "number";
    input.min = "0";
    input.value = run[field];
    input.dataset.rowKey = run.id;
    input.dataset.field = field;
    const commit = (value) => {
      const v = Math.max(0, parseInt(value, 10) || 0);
      input.value = v;
      updateRun({ run_id: run.id, [field]: v }).then(applyRow);
    };
    // The −/+ buttons are out of the tab order, so Tab runs bib → run → penalties.
    const minus = el("button", "pen-btn", "−");
    minus.type = "button";
    minus.tabIndex = -1;
    minus.addEventListener("click", () => commit((parseInt(input.value, 10) || 0) - 1));
    const plus = el("button", "pen-btn", "+");
    plus.type = "button";
    plus.tabIndex = -1;
    plus.addEventListener("click", () => commit((parseInt(input.value, 10) || 0) + 1));
    input.addEventListener("change", () => commit(input.value));
    wrap.append(minus, input, plus);
    return wrap;
  }

  const secs = (n) => (n ? `${n}s` : "0s");

  // ---- ignored times: start and finish rails, each beside where it falls ----
  function renderIgnored() {
    rail.querySelectorAll(".ignored-col").forEach((col) => {
      col.querySelectorAll(".ignored-chip").forEach((c) => c.remove());
      const role = col.dataset.role;
      const chips = state.ignored.filter((s) => s.role === role);
      col.classList.toggle("ignored-col--empty", chips.length === 0);
      chips.forEach((s) => col.append(ignoredChip(s)));
    });
    positionIgnored();
  }

  function ignoredChip(sig) {
    const chip = el("div", "ignored-chip" + (sig.manual ? " ignored-chip--manual" : ""), sig.time);
    chip.draggable = true;
    chip.dataset.signalId = sig.id;
    chip.dataset.role = sig.role;
    chip.dataset.time = sig.time;
    chip.title = "Drag onto a run's slot · double-click to restore";
    chip.addEventListener("dragstart", (e) => onDragStart(e, sig.id, sig.role, sig.time));
    chip.addEventListener("dragend", clearDrag);
    chip.addEventListener("dblclick", () => setIgnored(sig.id, false).then(refresh));
    return chip;
  }

  // Float each ignored chip vertically at the boundary between the two rows its
  // time falls between (rows never move); stack within a column to avoid overlaps.
  function positionIgnored() {
    const railTop = rail.getBoundingClientRect().top;
    const rowInfo = [...rowsEl.querySelectorAll("tr[data-run-id]")].map((tr) => {
      const rect = tr.getBoundingClientRect();
      const row = state.rows.find((r) => String(r.id) === tr.dataset.runId);
      const time = (row.start && row.start.time) || (row.finish && row.finish.time) || "";
      return { top: rect.top - railTop, bottom: rect.bottom - railTop, time };
    });
    rail.querySelectorAll(".ignored-col").forEach((col) => {
      let lastBottom = -Infinity;
      col.querySelectorAll(".ignored-chip").forEach((chip) => {
        const t = chip.dataset.time;
        let y = 0;
        if (rowInfo.length) {
          const idx = rowInfo.findIndex((ri) => ri.time && ri.time < t);
          if (idx === -1) y = rowInfo[rowInfo.length - 1].bottom;
          else if (idx === 0) y = rowInfo[0].top;
          else y = rowInfo[idx].top;
        }
        let top = y - chip.offsetHeight / 2;
        if (top < lastBottom + 4) top = lastBottom + 4;
        chip.style.top = `${Math.max(0, top)}px`;
        lastBottom = top + chip.offsetHeight;
      });
    });
  }

  // ---- apply a single updated row (edit reply) ----------------------------
  function applyRow(resp) {
    if (!resp || !resp.ok || !resp.row) return;
    const i = state.rows.findIndex((r) => r.id === resp.row.id);
    if (i !== -1) state.rows[i] = resp.row;
    const tr = rowsEl.querySelector(`tr[data-run-id="${resp.row.id}"]`);
    if (tr) tr.replaceWith(renderRow(resp.row));
    positionIgnored();
  }

  // ---- drag and drop ------------------------------------------------------
  let dragged = null; // { id, role, time }

  function onDragStart(e, id, role, time) {
    dragged = { id: Number(id), role, time };
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", String(id));
  }
  function clearDrag() {
    dragged = null;
  }

  function pairingValid(slot, targetRunId) {
    const row = state.rows.find((r) => r.id === Number(targetRunId));
    if (!row) return true;
    if (slot === "start" && row.finish) return dragged.time <= row.finish.time;
    if (slot === "finish" && row.start) return dragged.time >= row.start.time;
    return true;
  }

  function wiggle(node) {
    node.classList.remove("time-slot--reject");
    void node.offsetWidth;
    node.classList.add("time-slot--reject");
    setTimeout(() => node.classList.remove("time-slot--reject"), 450);
  }

  rowsEl.addEventListener("dragover", (e) => {
    const slot = e.target.closest(".time-slot");
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

  // Dropping a run's time onto the rail ignores it.
  rail.addEventListener("dragover", (e) => {
    if (dragged && !state.ignored.some((s) => s.id === dragged.id)) {
      e.preventDefault();
      rail.classList.add("ignored-rail--drop");
    }
  });
  rail.addEventListener("dragleave", () => rail.classList.remove("ignored-rail--drop"));
  rail.addEventListener("drop", (e) => {
    rail.classList.remove("ignored-rail--drop");
    if (dragged && !state.ignored.some((s) => s.id === dragged.id)) {
      e.preventDefault();
      setIgnored(dragged.id, true).then(refresh);
    }
  });

  // ---- live refresh (WebSocket nudge) -------------------------------------
  async function refresh() {
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

  window.addEventListener("resize", positionIgnored);
  render();
  connect();
})();
