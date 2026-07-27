// Live timing view. Renders runs (a start time paired with a finish time) newest
// first, with pre-enterable placeholder rows at the top; lets the operator enter
// bib/class/run/penalties, and drag a time to the Ignored panel (two columns,
// start and finish) to ignore it or back onto a run's slot to pair it. State lives
// on the server; edits POST and the view is nudged over a WebSocket to re-fetch.
(function () {
  "use strict";

  const URLS = window.TIMING_URLS;
  const CSRF = window.TIMING_CSRF;
  const dataEl = document.getElementById("timing-data");
  if (!URLS || !dataEl) return;

  let state = JSON.parse(dataEl.textContent);

  const rowsEl = document.getElementById("timing-rows");
  const emptyEl = document.getElementById("timing-empty");
  const ignoredBox = document.getElementById("ignored-box");
  const lockLabel = document.getElementById("input-lock");
  const lockCheck = document.getElementById("input-lock-check");
  const lockText = document.getElementById("input-lock-text");
  const lockNote = document.getElementById("input-lock-note");

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
  const setTime = (runId, slot, time) => postJSON(URLS.setTime, { run_id: runId, slot, time });
  const setRuntime = (runId, runTime) => postJSON(URLS.setRuntime, { run_id: runId, run_time: runTime });
  const setStatus = (runId, status) => postJSON(URLS.runStatus, { run_id: runId, status });
  const setInputLock = (locked) => postJSON(URLS.inputLock, { locked });

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
  // Start, Finish, Run time, Bib, Competitor, Class, Run, Status, Total
  // (+4 penalty cols).
  const colspan = () => (state.penalties_enabled ? 13 : 9);

  // ---- rendering ----------------------------------------------------------
  function render() {
    rowsEl.replaceChildren(addStrip(), ...state.rows.map(renderRow));
    emptyEl.hidden = state.rows.length > 0;
    renderIgnored();
    renderLock();
    if (window.renderDeviceAlarm) window.renderDeviceAlarm(state.device_link);
    if (window.renderBarrierPhase) window.renderBarrierPhase(state.barrier);
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

  // A thin strip below the header; hover reveals a + to add a placeholder row.
  function addStrip() {
    const tr = el("tr", "tt-add-strip");
    const td = el("td");
    td.colSpan = colspan();
    const btn = el("button", "tt-add-btn", "+");
    btn.type = "button";
    btn.title = gettext("Add a row for an upcoming starter");
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
    // A run closed with a state code is not going to produce a time — the row
    // says so at a glance rather than only in its dropdown.
    if (run.status) tr.classList.add("timing-row--status");

    tr.append(cell("tt-time", timeSlot(row.start, "start", run.id)));
    tr.append(cell("tt-time", timeSlot(row.finish, "finish", run.id)));
    tr.append(runTimeCell(row));
    tr.append(cell("tt-bib", bibField(run)));
    tr.append(nameCell(run));
    tr.append(cell("tt-class", classField(run)));
    tr.append(cell("tt-run-sel", runField(run)));
    tr.append(cell("tt-status", statusField(run)));
    if (state.penalties_enabled) {
      tr.append(cell("tt-pen", penaltyBox(run, "pylon_count")));
      tr.append(cell("tt-pen", penaltyBox(run, "task_count")));
      tr.append(cell("tt-pen", penaltyBox(run, "stopline_count")));
      tr.append(cell("tt-pen-total", el("span", "pen-total", run.penalty_text || "–")));
    }
    // A run closed with a state code has no total to show, so the cell carries
    // the code — the one place the row's outcome is read from either way.
    const totalCell = cell("tt-total", run.status
      ? el("span", "run-total run-total--status", run.status.toUpperCase())
      : el("span", "run-total", run.total || "–"));
    // An empty placeholder can be removed.
    if (row.placeholder) {
      const del = el("button", "row-delete", "×");
      del.type = "button";
      del.tabIndex = -1;
      del.title = gettext("Remove this row");
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
    // Double-click the slot (whether it holds a time or not) to type a time in by
    // hand — for when the device didn't fire. Ignoring a wrong time is a drag to
    // the Ignored panel.
    wrap.addEventListener("dblclick", () =>
      enterTimeEdit(wrap, role, runId, sig ? sig.time : ""));
    if (!sig) {
      wrap.classList.add("time-slot--empty");
      wrap.append(el("span", "time-empty", "–"));
      wrap.title = gettext("Double-click to type a time");
      return wrap;
    }
    wrap.dataset.time = sig.time;
    let cls = "time-chip";
    if (sig.entered) cls += " time-chip--entered";
    else if (sig.manual) cls += " time-chip--manual";
    const chip = el("span", cls, sig.time);
    chip.draggable = true;
    chip.dataset.signalId = sig.id;
    chip.dataset.role = role;
    chip.dataset.time = sig.time;
    const kind = sig.entered ? gettext("Typed in by hand") : sig.manual ? gettext("Manual") : gettext("Light barrier");
    chip.title = interpolate(gettext("%(kind)s · drag to pair or to the Ignored panel · double-click to edit"), { kind: kind }, true);
    chip.addEventListener("dragstart", (e) => onDragStart(e, sig.id, role, sig.time));
    chip.addEventListener("dragend", clearDrag);
    wrap.append(chip);
    return wrap;
  }

  // The Run time cell (not Total): editable by double-click, and highlighted when
  // the value was typed in rather than measured.
  function runTimeCell(row) {
    const span = el("span", "run-time" + (row.run_time_manual ? " run-time--entered" : ""),
      row.run_time || "–");
    const td = cell("tt-run", span);
    td.title = gettext("Double-click to type a run time");
    td.addEventListener("dblclick", () =>
      enterRuntimeEdit(td, row.run.id, row.run_time_manual ? row.run_time : ""));
    return td;
  }

  // Swap a slot/cell for a text input pre-filled with the current value; Enter (or
  // blur) commits, Escape cancels. The reply re-renders the row, clearing the box.
  function inlineEdit(host, current, placeholder, onCommit) {
    if (host.querySelector(".time-edit")) return;
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

  function enterTimeEdit(wrap, role, runId, current) {
    inlineEdit(wrap, current, "hh:mm:ss.xx", (value) =>
      setTime(runId, role, value).then((resp) => {
        // A rejected time (start after its finish) leaves the row unchanged — just
        // re-render to drop the edit box.
        if (!resp || !resp.ok) refresh();
        else applyRow(resp);
      }));
  }

  function enterRuntimeEdit(td, runId, current) {
    inlineEdit(td, current, "s.xx", (value) => setRuntime(runId, value).then(applyRow));
  }

  function bibField(run) {
    const wrap = el("div", "bib-field");
    const input = el("input", "bib-input" + (run.bib_unknown ? " bib-input--unknown" : ""));
    input.type = "number";
    input.min = "1";
    input.value = run.bib_number == null ? "" : run.bib_number;
    input.dataset.rowKey = run.id;
    input.dataset.field = "bib";
    if (run.bib_unknown) input.title = gettext("No starter with this bib is registered (kept anyway).");

    let lastSent = input.value;
    const submit = () => {
      if (input.value === lastSent) return Promise.resolve(null);
      lastSent = input.value;
      return updateRun({ run_id: run.id, bib_number: input.value });
    };
    input.addEventListener("change", () => submit().then(applyRow));
    // Tab out of the bib field lands on the Class dropdown (if the participant is
    // multi-class — the dropdown only exists after the bib resolves), else the
    // Run field. We drive it ourselves so it isn't skipped before the row renders.
    input.addEventListener("keydown", (e) => {
      if (e.key !== "Tab" || e.shiftKey) return;
      e.preventDefault();
      submit().then((resp) => {
        applyRow(resp);
        const tr = rowsEl.querySelector(`tr[data-run-id="${run.id}"]`);
        const target = tr && (tr.querySelector(".class-select") || tr.querySelector(".run-select"));
        if (target) target.focus();
      });
    });
    wrap.append(input);
    return wrap;
  }

  // The competitor's name, in its own flexible column. An unresolved bib says so
  // in words rather than leaving the cell blank next to a red input.
  function nameCell(run) {
    const td = cell("tt-name");
    if (run.bib_unknown) {
      const warn = el("span", "bib-name bib-name--unknown", gettext("Not registered"));
      warn.title = gettext("No starter with this bib is registered (kept anyway).");
      td.append(warn);
    } else {
      const name = el("span", "bib-name", run.name || "");
      if (run.name) name.title = run.name;
      td.append(name);
    }
    return td;
  }

  function classField(run) {
    // A dropdown when the participant has more than one class slot (distinct
    // classes, or the same class entered more than once — "Klasse 2 (1)/(2)").
    if (run.class_options.length > 1) {
      const select = el("select", "class-select");
      select.dataset.rowKey = run.id;
      select.dataset.field = "class";
      run.class_options.forEach((opt) => {
        const o = el("option", null, opt.label);
        o.value = opt.value;
        // A class whose runs are all done is disabled (unless it's the current one).
        if (opt.disabled && opt.value !== run.class_key) o.disabled = true;
        if (opt.value === run.class_key) o.selected = true;
        select.append(o);
      });
      select.addEventListener("change", () =>
        updateRun({ run_id: run.id, class_key: select.value }).then(applyRow)
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

  // How a run ended when it didn't end in a time: DNF / DNC / DNS / DSQ, or "–"
  // for the ordinary case. The codes come from the server (runstatus.options) so
  // the page can't invent one the model doesn't have.
  function statusField(run) {
    const select = el("select", "status-select" + (run.status ? " status-select--set" : ""));
    select.dataset.rowKey = run.id;
    select.dataset.field = "status";
    const blank = el("option", null, "–");
    blank.value = "";
    select.append(blank);
    (state.status_options || []).forEach((opt) => {
      const o = el("option", null, opt.label);
      o.value = opt.value;
      o.title = opt.title;
      if (opt.value === run.status) o.selected = true;
      select.append(o);
    });
    select.title = run.status
      ? gettext("This run is closed with a state code — it is not scored.")
      : gettext("Close this run with a state code instead of a time");
    select.addEventListener("change", () =>
      setStatus(run.id, select.value).then(applyRow)
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
    // In marshal mode the marshal posts own an auto-bound run's penalty and its
    // own counts are ignored — so the stepper is shown disabled with the reason,
    // rather than accepting a number that changes nothing (see own_counts_apply).
    const editable = run.penalties_editable !== false;
    if (!editable) {
      wrap.classList.add("pen-stepper--locked");
      wrap.title = gettext("The marshal posts enter this run's penalties.");
    }
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
    if (!editable) [minus, input, plus].forEach((node) => { node.disabled = true; });
    wrap.append(minus, input, plus);
    return wrap;
  }

  // ---- ignored times ------------------------------------------------------
  // The rail itself is shared with Auto timing (static/js/ignored_panel.js); this
  // view only supplies the drag/restore behaviour that differs between them.
  const ignoredPanel = IgnoredPanel.create({
    box: ignoredBox,
    countEl: document.getElementById("ignored-count"),
    moreEl: document.getElementById("ignored-more"),
    onDragStart: (e, id, role, time) => onDragStart(e, id, role, time),
    onDragEnd: () => clearDrag(),
    onRestore: (id) => setIgnored(id, false).then(refresh),
  });

  function renderIgnored() {
    ignoredPanel.render(state.ignored, state.ignored_split);
  }

  // ---- apply a single updated row (edit reply) ----------------------------
  function applyRow(resp) {
    if (!resp || !resp.ok || !resp.row) return;
    const i = state.rows.findIndex((r) => r.id === resp.row.id);
    if (i !== -1) state.rows[i] = resp.row;
    const tr = rowsEl.querySelector(`tr[data-run-id="${resp.row.id}"]`);
    if (tr) tr.replaceWith(renderRow(resp.row));
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

  // Dropping a run's time onto the Ignored panel ignores it.
  ignoredBox.addEventListener("dragover", (e) => {
    if (dragged && !state.ignored.some((s) => s.id === dragged.id)) {
      e.preventDefault();
      ignoredBox.classList.add("ignored-box--drop");
    }
  });
  ignoredBox.addEventListener("dragleave", () => ignoredBox.classList.remove("ignored-box--drop"));
  ignoredBox.addEventListener("drop", (e) => {
    ignoredBox.classList.remove("ignored-box--drop");
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
    let data;
    try {
      data = await fetch(URLS.arrangement).then((r) => r.json());
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

  if (lockCheck) {
    lockCheck.addEventListener("change", () => {
      state.input_locked = lockCheck.checked;   // optimistic; the nudge confirms
      renderLock();
      setInputLock(lockCheck.checked).then(refresh);
    });
  }

  render();
  // The socket, its reconnect/backoff, the connection banner and the re-fetch
  // after an outage all live in live_socket.js — shared with the Auto timing,
  // Marshal Posts and Dashboard views.
  window.liveSocket({ onRefresh: refresh });
})();
