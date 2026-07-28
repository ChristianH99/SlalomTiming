/* Competition Setup > Run order: dragging classes into runs, and the start
 * pattern editor with its live preview (which re-implements the expansion in
 * apps/competitions/startpattern.py in the browser).
 *
 * Was inline in its template; see static/js/shell.js for why nothing is. Two
 * consequences: the page's numbers arrive through `json_script`, and its strings
 * go through `gettext()` (the djangojs catalog base.html loads) rather than
 * `{% trans %}`.
 */

(function () {
  const runOrderEl = document.getElementById("run-order");
  const runOrderData = document.getElementById("run-order-data");
  if (!runOrderEl) return;
  const groups = JSON.parse(document.getElementById("run-groups-data").textContent);

  let dragChip = null;
  let dragRun = null;
  let ready = false;  // suppress dirty-marking during the initial build

  function makeChip(pk, label) {
    const chip = document.createElement("span");
    chip.className = "class-chip";
    chip.draggable = true;
    chip.setAttribute("data-pk", pk);
    chip.textContent = label || "?";
    chip.addEventListener("dragstart", (e) => {
      dragChip = chip; chip.classList.add("dragging");
      runOrderEl.classList.add("chip-dragging");  // fattens gaps as drop targets
      e.dataTransfer.effectAllowed = "move";
    });
    chip.addEventListener("dragend", () => {
      chip.classList.remove("dragging");
      runOrderEl.classList.remove("chip-dragging");
      runOrderEl.querySelectorAll(".gap-active, .zone-over")
        .forEach((el) => el.classList.remove("gap-active", "zone-over"));
      dragChip = null;
      pruneEmptyRuns();  // a run left with no classes disappears
      syncGaps(); serialize();
    });
    return chip;
  }

  function makeRun() {
    const run = document.createElement("div");
    run.className = "run-row";
    run.setAttribute("data-run", "");

    const handle = document.createElement("span");
    handle.className = "run-handle";
    handle.textContent = "⠿";
    handle.title = gettext("Drag to reorder run");
    handle.addEventListener("mousedown", () => { run.draggable = true; });
    run.addEventListener("dragstart", (e) => {
      // Let chip drags through — only react to a handle-initiated row drag.
      if (e.target.closest(".class-chip")) return;
      if (!run.draggable) { e.preventDefault(); return; }
      dragRun = run; run.classList.add("run-dragging");
      e.dataTransfer.effectAllowed = "move";
    });
    run.addEventListener("dragend", () => {
      run.classList.remove("run-dragging"); run.draggable = false; dragRun = null;
      syncGaps(); serialize();
    });

    const label = document.createElement("span");
    label.className = "run-label";

    const zone = document.createElement("div");
    zone.className = "run-zone";
    zone.setAttribute("data-zone", "");
    zone.addEventListener("dragover", (e) => {
      if (!dragChip) return;
      e.preventDefault(); zone.classList.add("zone-over");
      // Live-move the dragged chip to its position under the cursor so a
      // drop both groups it into this run and orders it within the run.
      const after = chipAfter(zone, e.clientX);
      if (after) zone.insertBefore(dragChip, after);
      else zone.appendChild(dragChip);
    });
    zone.addEventListener("dragleave", () => zone.classList.remove("zone-over"));
    zone.addEventListener("drop", (e) => {
      if (!dragChip) return;
      e.preventDefault(); zone.classList.remove("zone-over");
      serialize();  // chip is already in place from the dragover live-move
    });

    run.appendChild(handle);
    run.appendChild(label);
    run.appendChild(zone);
    return run;
  }

  function relabelRuns() {
    runOrderEl.querySelectorAll("[data-run]").forEach((run, i) => {
      run.querySelector(".run-label").textContent = gettext("Run") + " " + (i + 1);
    });
  }

  // Which chip in `zone` sits just right of cursor x (insert the drag before it).
  function chipAfter(zone, x) {
    let after = null, afterOffset = -Infinity;
    zone.querySelectorAll(".class-chip:not(.dragging)").forEach((chip) => {
      const box = chip.getBoundingClientRect();
      const offset = x - (box.left + box.width / 2);
      if (offset < 0 && offset > afterOffset) { afterOffset = offset; after = chip; }
    });
    return after;
  }

  // Thin drop target between runs: dropping a class here spawns a new run.
  function makeGap() {
    const gap = document.createElement("div");
    gap.className = "run-gap";
    gap.addEventListener("dragover", (e) => {
      if (!dragChip) return;
      e.preventDefault(); gap.classList.add("gap-active");
    });
    gap.addEventListener("dragleave", () => gap.classList.remove("gap-active"));
    gap.addEventListener("drop", (e) => {
      if (!dragChip) return;
      e.preventDefault(); gap.classList.remove("gap-active");
      const run = makeRun();
      runOrderEl.insertBefore(run, gap);  // new run takes this slot
      run.querySelector("[data-zone]").appendChild(dragChip);
      syncGaps(); serialize();
    });
    return gap;
  }

  // Drop the run if it no longer holds any classes.
  function pruneEmptyRuns() {
    runOrderEl.querySelectorAll("[data-run]").forEach((run) => {
      if (!run.querySelector(".class-chip")) run.remove();
    });
  }

  // Refresh the gap elements: one before every run and one after the last.
  function syncGaps() {
    runOrderEl.querySelectorAll(".run-gap").forEach((g) => g.remove());
    runOrderEl.querySelectorAll("[data-run]").forEach((row) => {
      runOrderEl.insertBefore(makeGap(), row);
    });
    runOrderEl.appendChild(makeGap());
  }

  // Reorder runs: drop a dragged run before the run under the cursor.
  runOrderEl.addEventListener("dragover", (e) => {
    if (!dragRun) return;
    e.preventDefault();
    const after = [...runOrderEl.querySelectorAll("[data-run]:not(.run-dragging)")]
      .find((r) => e.clientY <= r.getBoundingClientRect().top + r.offsetHeight / 2);
    if (after) runOrderEl.insertBefore(dragRun, after);
    else runOrderEl.appendChild(dragRun);
  });

  function serialize() {
    const runs = [];
    runOrderEl.querySelectorAll("[data-run]").forEach((run) => {
      const pks = [...run.querySelectorAll(".class-chip")]
        .map((c) => parseInt(c.getAttribute("data-pk"), 10));
      if (pks.length) runs.push(pks);
    });
    runOrderData.value = JSON.stringify(runs);
    relabelRuns();
    // The start-pattern preview expands over each run's participants, so it
    // has to follow the grouping live as classes are dragged around.
    document.dispatchEvent(new Event("run-order-change"));
    if (ready) document.dispatchEvent(new Event("unsaved-change"));
  }

  // Build the widget from the server-provided run groups.
  groups.forEach((group) => {
    const run = makeRun();
    const zone = run.querySelector("[data-zone]");
    group.forEach((c) => zone.appendChild(makeChip(c.pk, c.name)));
    runOrderEl.appendChild(run);
  });
  syncGaps();
  serialize();
  ready = true;  // subsequent serialize() calls come from user edits
})();

(function () {
  const patternEl = document.getElementById("start-pattern");
  const patternData = document.getElementById("start-pattern-data");
  const previewEl = document.getElementById("pattern-preview");
  const runOrderData = document.getElementById("run-order-data");
  if (!patternEl) return;

  const startersByClass = JSON.parse(document.getElementById("starters-data").textContent);
  const blocks = JSON.parse(document.getElementById("start-pattern-json").textContent);
  const dummyToggle = document.querySelector("[data-dummy-toggle]");
  const dummyCountInput = document.querySelector("[data-dummy-count]");
  const dummyCountWrap = document.querySelector("[data-dummy-count-wrap]");
  const LABELS = {practice: gettext("Practice"), counted: gettext("Counted")};
  const SHORT = {practice: gettext("P"), counted: gettext("C")};
  const MAX_WINDOW = 99;
  const MAX_DUMMY = (window.pageData("page-config") || {}).maxDummy;

  // Run counts per class, for dummy starters — the real starters carry their
  // own, but there are none to read from before anyone is registered.
  const classesByPk = {};
  JSON.parse(document.getElementById("run-groups-data").textContent)
    .forEach((run) => run.forEach((c) => { classesByPk[String(c.pk)] = c; }));

  let dragChip = null;
  let dragBlock = null;
  let ready = false;

  /* ---- expansion (mirrors apps/competitions/startpattern.py:passes) ----
     Duplicated in JS on purpose: the preview has to redraw on every drag,
     and Python stays the source of truth for what actually gets saved. */
  function expand(blockList, starters) {
    const taken = new Map();   // "key|runType" -> runs of that type used
    const used = (k) => taken.get(k) || 0;
    return blockList.map((block) => {
      const size = block.window || starters.length;
      const passes = [];
      for (let start = 0; start < starters.length; start += size) {
        const window = starters.slice(start, start + size);
        const slots = [];
        block.chips.forEach((runType) => {
          window.forEach((starter) => {
            const k = starter.key + "|" + runType;
            const allowance = runType === "practice" ? starter.practice : starter.counted;
            if (used(k) >= allowance) return;  // no run of this type left
            const runNumber = used(k) + 1;
            taken.set(k, runNumber);
            slots.push({starter: starter, runType: runType, runNumber: runNumber});
          });
        });
        passes.push(slots);
      }
      return passes;
    });
  }

  // Class pks grouped into runs, read live from the run-order widget.
  function currentRunPks() {
    try { return JSON.parse(runOrderData.value) || []; } catch (e) { return []; }
  }

  // Runs of real starters: whoever is registered, per the live grouping.
  function realRuns() {
    return currentRunPks().map((pks) => {
      const starters = [];
      pks.forEach((pk) => (startersByClass[String(pk)] || []).forEach((s) => starters.push(s)));
      // Participants of every class in a run start together, ordered by bib.
      starters.sort((a, b) => a.bib - b.bib || a.class_name.localeCompare(b.class_name));
      return starters;
    });
  }

  // A single made-up run of `count` starters, bibs 1..count. Used to check a
  // pattern before anyone is registered. They take the runs the first class
  // of the first run grants, since that's what a real starter there would get.
  function dummyRun(count) {
    const firstRun = currentRunPks()[0] || [];
    const cls = classesByPk[String(firstRun[0])];
    const practice = cls ? cls.practice : 1;
    const counted = cls ? cls.counted : 2;
    const starters = [];
    for (let bib = 1; bib <= count; bib++) {
      starters.push({
        key: "dummy-" + bib, bib: bib, name: gettext("Dummy participant") + " " + bib,
        class_name: cls ? cls.name : "—", practice: practice, counted: counted,
      });
    }
    return {starters: starters, cls: cls};
  }

  function dummyCount() {
    const raw = parseInt(dummyCountInput.value, 10);
    if (Number.isNaN(raw) || raw < 1) return 1;
    return Math.min(raw, MAX_DUMMY);
  }

  /* ---- pattern builder ---- */
  function makeRunChip(runType) {
    const chip = document.createElement("span");
    chip.className = "run-chip run-chip--" + runType;
    chip.draggable = true;
    chip.setAttribute("data-run-type", runType);
    chip.textContent = LABELS[runType];

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "run-chip-remove";
    remove.textContent = "×";
    remove.title = gettext("Remove this run");
    remove.addEventListener("click", () => { chip.remove(); serialize(); });
    chip.appendChild(remove);

    chip.addEventListener("dragstart", (e) => {
      dragChip = chip; chip.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      e.stopPropagation();  // don't start a block drag as well
    });
    chip.addEventListener("dragend", () => {
      chip.classList.remove("dragging"); dragChip = null;
      patternEl.querySelectorAll(".zone-over").forEach((el) => el.classList.remove("zone-over"));
      serialize();
    });
    return chip;
  }

  function makeBlock(block) {
    const row = document.createElement("div");
    row.className = "pattern-block";
    row.setAttribute("data-block", "");

    const handle = document.createElement("span");
    handle.className = "run-handle";
    handle.textContent = "⠿";
    handle.title = gettext("Drag to reorder block");
    handle.addEventListener("mousedown", () => { row.draggable = true; });
    row.addEventListener("dragstart", (e) => {
      if (e.target.closest(".run-chip")) return;  // let chip drags through
      if (!row.draggable) { e.preventDefault(); return; }
      dragBlock = row; row.classList.add("run-dragging");
      e.dataTransfer.effectAllowed = "move";
    });
    row.addEventListener("dragend", () => {
      row.classList.remove("run-dragging"); row.draggable = false; dragBlock = null;
      serialize();
    });

    const label = document.createElement("span");
    label.className = "run-label";

    // Window size: how many participants the block takes per pass. Reads as
    // "2 at a time" or "all at a time" — the checkbox is the source of truth
    // for a whole-field window, since typing that into a number field is
    // fiddly. A div, not a label: it holds the "all" label, and labels can't nest.
    const windowWrap = document.createElement("div");
    windowWrap.className = "pattern-window";
    const windowInput = document.createElement("input");
    windowInput.type = "number";
    windowInput.min = "1";
    windowInput.max = String(MAX_WINDOW);
    windowInput.title = gettext("Participants per pass");
    windowInput.setAttribute("data-window", "");
    windowInput.addEventListener("input", serialize);

    const allWrap = document.createElement("label");
    allWrap.className = "pattern-all";
    const allInput = document.createElement("input");
    allInput.type = "checkbox";
    allInput.setAttribute("data-window-all", "");
    allInput.title = gettext("Take every participant in one pass");
    allInput.checked = !(block && block.window);  // a new block starts as "all"
    allWrap.appendChild(allInput);
    allWrap.appendChild(Object.assign(document.createElement("span"), {
      textContent: gettext("all"),
    }));

    windowWrap.appendChild(windowInput);
    windowWrap.appendChild(allWrap);
    windowWrap.appendChild(Object.assign(document.createElement("span"), {
      className: "pattern-window-suffix", textContent: gettext("at a time"),
    }));

    // Ticking "all" empties and disables the number; unticking restores a
    // usable number so the block never sits in a blank in-between state.
    function syncWindow() {
      windowInput.disabled = allInput.checked;
      if (allInput.checked) windowInput.value = "";
      else if (!windowInput.value) windowInput.value = "2";
    }
    allInput.addEventListener("change", () => { syncWindow(); serialize(); });
    windowInput.value = block && block.window ? block.window : "";
    syncWindow();

    const zone = document.createElement("div");
    zone.className = "run-zone";
    zone.setAttribute("data-zone", "");
    zone.addEventListener("dragover", (e) => {
      if (!dragChip) return;
      e.preventDefault(); zone.classList.add("zone-over");
      // Live-move the chip under the cursor, so a drop both puts it in this
      // block and orders it within the block. A palette chip is copied in.
      if (dragChip.hasAttribute("data-palette")) {
        dragChip = makeRunChip(dragChip.getAttribute("data-run-type"));
        dragChip.classList.add("dragging");
      }
      const after = chipAfter(zone, e.clientX);
      if (after) zone.insertBefore(dragChip, after);
      else zone.appendChild(dragChip);
    });
    zone.addEventListener("dragleave", () => zone.classList.remove("zone-over"));
    zone.addEventListener("drop", (e) => {
      if (!dragChip) return;
      e.preventDefault(); zone.classList.remove("zone-over");
      serialize();  // the chip is already in place from the dragover live-move
    });

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "row-remove";
    remove.textContent = "×";
    remove.title = gettext("Remove this block");
    remove.addEventListener("click", () => { row.remove(); serialize(); });

    row.appendChild(handle);
    row.appendChild(label);
    row.appendChild(windowWrap);
    row.appendChild(zone);
    row.appendChild(remove);
    if (block) block.chips.forEach((c) => zone.appendChild(makeRunChip(c)));
    return row;
  }

  // Which chip in `zone` sits just right of cursor x (insert the drag before it).
  function chipAfter(zone, x) {
    let after = null, afterOffset = -Infinity;
    zone.querySelectorAll(".run-chip:not(.dragging)").forEach((chip) => {
      const box = chip.getBoundingClientRect();
      const offset = x - (box.left + box.width / 2);
      if (offset < 0 && offset > afterOffset) { afterOffset = offset; after = chip; }
    });
    return after;
  }

  // Reorder blocks: drop a dragged block before the block under the cursor.
  patternEl.addEventListener("dragover", (e) => {
    if (!dragBlock) return;
    e.preventDefault();
    const after = [...patternEl.querySelectorAll("[data-block]:not(.run-dragging)")]
      .find((r) => e.clientY <= r.getBoundingClientRect().top + r.offsetHeight / 2);
    if (after) patternEl.insertBefore(dragBlock, after);
    else patternEl.appendChild(dragBlock);
  });

  // Palette chips are templates — dragging one clones it into a block.
  document.querySelectorAll("[data-palette]").forEach((chip) => {
    chip.addEventListener("dragstart", (e) => {
      dragChip = chip;
      e.dataTransfer.effectAllowed = "copy";
    });
    chip.addEventListener("dragend", () => {
      // dragChip is the clone once it has been dragged into a block; the
      // clone gets no dragend of its own, so clear its drag state here.
      if (dragChip && dragChip !== chip) dragChip.classList.remove("dragging");
      dragChip = null;
      patternEl.querySelectorAll(".zone-over").forEach((el) => el.classList.remove("zone-over"));
    });
  });

  document.querySelector("[data-add-block]").addEventListener("click", () => {
    patternEl.appendChild(makeBlock(null));
    serialize();
  });

  function serialize() {
    const out = [];
    patternEl.querySelectorAll("[data-block]").forEach((row, i) => {
      row.querySelector(".run-label").textContent = gettext("Block") + " " + (i + 1);
      const chips = [...row.querySelectorAll(".run-chip")]
        .map((c) => c.getAttribute("data-run-type"));
      const raw = parseInt(row.querySelector("[data-window]").value, 10);
      const all = row.querySelector("[data-window-all]").checked;
      const window = all || Number.isNaN(raw) || raw < 1
        ? null : Math.min(raw, MAX_WINDOW);
      if (chips.length) out.push({window: window, chips: chips});
    });
    patternData.value = JSON.stringify(out);
    renderPreview();
    if (ready) document.dispatchEvent(new Event("unsaved-change"));
  }

  /* ---- preview ---- */
  function renderPreview() {
    previewEl.textContent = "";
    let pattern = [];
    try { pattern = JSON.parse(patternData.value) || []; } catch (e) { pattern = []; }

    if (!pattern.length) {
      previewEl.appendChild(emptyNote(gettext("Add a block to see the start order.")));
      return;
    }

    // Dummy mode checks the pattern's shape, so it shows one made-up run
    // instead of the real per-run breakdown.
    const dummy = dummyToggle.checked;
    const made = dummy ? dummyRun(dummyCount()) : null;
    const runs = dummy ? [made.starters] : realRuns();

    runs.forEach((starters, i) => {
      const card = document.createElement("div");
      card.className = "preview-run";

      const head = document.createElement("div");
      head.className = "preview-run-head";
      head.textContent = dummy ? gettext("Dummy run") : gettext("Run") + " " + (i + 1);
      card.appendChild(head);

      if (dummy) {
        // The dummy starters' run counts are inherited, not invented — say so.
        const note = document.createElement("div");
        note.className = "preview-run-note";
        note.textContent = made.cls
          ? gettext("{n} participants taking {cls}’s runs: {p} practice, {c} counted")
              .replace("{n}", starters.length).replace("{cls}", made.cls.name)
              .replace("{p}", made.cls.practice).replace("{c}", made.cls.counted)
          : gettext("{n} participants taking 1 practice, 2 counted")
              .replace("{n}", starters.length);
        card.appendChild(note);
      }

      if (!starters.length) {
        card.appendChild(emptyNote(gettext("No participants with a bib in this run yet.")));
        previewEl.appendChild(card);
        return;
      }

      const expanded = expand(pattern, starters);
      expanded.forEach((passes, b) => {
        const line = document.createElement("div");
        line.className = "preview-line";
        const tag = document.createElement("span");
        tag.className = "preview-line-tag";
        tag.textContent = gettext("Block") + " " + (b + 1);
        line.appendChild(tag);

        const seq = document.createElement("div");
        seq.className = "preview-seq";
        // Passes that schedule nobody are dropped, so no stray separators
        // appear around them. A block with no pass left at all gets a note
        // rather than a blank row — that happens when every starter has
        // already used up the runs it asks for.
        const filled = passes.filter((slots) => slots.length);
        if (!filled.length) {
          seq.appendChild(Object.assign(document.createElement("span"), {
            className: "preview-nothing",
            textContent: gettext("nothing left to schedule — these participants have no such run left"),
          }));
        }
        filled.forEach((slots, p) => {
          if (p) seq.appendChild(Object.assign(document.createElement("span"), {
            className: "preview-sep", textContent: "|",  // one window pass ends
          }));
          slots.forEach((slot) => {
            const chip = document.createElement("span");
            chip.className = "preview-slot preview-slot--" + slot.runType;
            chip.textContent = "#" + slot.starter.bib + " " + SHORT[slot.runType] + slot.runNumber;
            chip.title = gettext("{name} — {cls}, {type} run {n}")
              .replace("{name}", slot.starter.name).replace("{cls}", slot.starter.class_name)
              .replace("{type}", LABELS[slot.runType].toLowerCase()).replace("{n}", slot.runNumber);
            seq.appendChild(chip);
          });
        });
        line.appendChild(seq);
        card.appendChild(line);
      });

      const missing = shortfalls(pattern, starters);
      if (missing.length) {
        const warn = document.createElement("div");
        warn.className = "preview-warning";
        warn.textContent = gettext("Not scheduled: {list}.").replace("{list}", missing.join("; "));
        card.appendChild(warn);
      }
      previewEl.appendChild(card);
    });
  }

  // Starters whose class grants runs the pattern never plays, described for
  // the warning line. Mirrors startpattern.shortfalls.
  function shortfalls(pattern, starters) {
    const scheduled = new Map();
    expand(pattern, starters).forEach((passes) => passes.forEach((slots) => slots.forEach((slot) => {
      const k = slot.starter.key + "|" + slot.runType;
      scheduled.set(k, (scheduled.get(k) || 0) + 1);
    })));
    const out = [];
    starters.forEach((starter) => {
      const owed = [];
      ["practice", "counted"].forEach((runType) => {
        const allowance = runType === "practice" ? starter.practice : starter.counted;
        const short = allowance - (scheduled.get(starter.key + "|" + runType) || 0);
        if (short > 0) owed.push(short + " " + LABELS[runType].toLowerCase());
      });
      if (owed.length) out.push("#" + starter.bib + " (" + owed.join(", ") + ")");
    });
    return out;
  }

  function emptyNote(text) {
    const note = document.createElement("div");
    note.className = "preview-empty";
    note.textContent = text;
    return note;
  }

  // Preview source controls. These only redraw the preview — they're not a
  // pattern edit, so they must not mark the form dirty or be saved.
  function syncDummyControls() {
    dummyCountWrap.hidden = !dummyToggle.checked;
  }
  dummyToggle.addEventListener("change", () => { syncDummyControls(); renderPreview(); });
  dummyCountInput.addEventListener("input", renderPreview);
  syncDummyControls();

  // Build from the stored pattern, then follow the run-order widget.
  blocks.forEach((block) => patternEl.appendChild(makeBlock(block)));
  document.addEventListener("run-order-change", renderPreview);
  serialize();
  ready = true;
})();
