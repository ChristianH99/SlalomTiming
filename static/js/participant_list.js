/* The participant list: its search filter, the expandable per-row detail, and
 * the inline bib and whole-event-disqualification controls.
 *
 * Was inline in its template; see static/js/shell.js for why nothing is.
 */

(function () {
  const input = document.getElementById("participant-search-input");
  const body = document.getElementById("participants-body");
  if (!input || !body) return;

  const noResults = body.querySelector(".no-results-row");
  const rows = Array.from(body.querySelectorAll("tr.participant-row"));
  const detailFor = (pk) => body.querySelector('tr[data-detail-for="' + pk + '"]');

  // ---- expand / collapse (accordion: at most one open) ------------------
  const URLS = (window.pageData("page-config") || {}).urls || {};
  let openRow = null;

  // Order rows the way the server does: bibs first in bib order, then whoever
  // has drawn a number and is waiting for one, then the rest by name. Called
  // after a number changes so the row jumps to its place without a reload —
  // which means this comparison and the query's ORDER BY have to agree.
  function numberKey(row, selector) {
    const cell = row.querySelector(selector);
    if (!cell) return Infinity;                  // no such column on this page
    const n = parseInt(cell.textContent.trim(), 10);
    return Number.isNaN(n) ? Infinity : n;       // "–": nothing assigned
  }
  function resort() {
    const ordered = rows.slice().sort((a, b) => {
      const ka = numberKey(a, "[data-bib-display]"), kb = numberKey(b, "[data-bib-display]");
      if (ka !== kb) return ka - kb;
      const da = numberKey(a, "[data-draw-display]"), db = numberKey(b, "[data-draw-display]");
      if (da !== db) return da - db;
      const la = a.dataset.last || "", lb = b.dataset.last || "";
      if (la !== lb) return la < lb ? -1 : 1;
      const fa = a.dataset.first || "", fb = b.dataset.first || "";
      return fa < fb ? -1 : fa > fb ? 1 : 0;
    });
    for (const row of ordered) {
      body.appendChild(row);                     // appendChild moves the node
      const detail = detailFor(row.dataset.participant);
      if (detail) body.appendChild(detail);
    }
    if (noResults) body.appendChild(noResults);  // keep the sentinel last
  }

  // The detail panel's two inline number fields. One saver drives both: they
  // differ only in where they post, what the row shows afterwards, and whether
  // changing one has to be agreed to first — so those are the fields of a spec
  // rather than a second copy of the function.
  const FIELDS = [
    {
      key: "draw",
      input: ".pd-draw-input",
      msg: "[data-draw-msg]",
      display: "[data-draw-display]",
      url: () => URLS.setDraw,
      errorClass: "pd-draw-input--error",
      // A drawn number owns no times, so changing one asks nothing — but it
      // does move the row, because drawing a number is what lifts somebody out
      // of the register and into the event.
      resort: true,
    },
    {
      key: "bib",
      input: ".pd-bib-input",
      msg: "[data-bib-msg]",
      display: "[data-bib-display]",
      url: () => URLS.setBib,
      errorClass: "pd-bib-input--error",
      confirmTitle: () => gettext("Change the bib?"),
      confirmAccept: () => gettext("Change the bib"),
      resort: true,
    },
  ];

  // Rebuilt from whatever the row is currently showing rather than appended to,
  // so saving one field cannot drop the other one out of the search index.
  function refreshSearch(row) {
    const parts = [row.dataset.searchBase];
    for (const spec of FIELDS) {
      const cell = row.querySelector(spec.display);
      const text = cell ? cell.textContent.trim() : "";
      if (text && text !== "–") parts.push(text);
    }
    row.dataset.search = parts.join(" ");
  }

  // Persist one field. Called on blur and before collapsing. Returns true when
  // there was nothing to save or the save succeeded, false when the server
  // rejected it (so callers can keep the panel open).
  async function saveField(row, spec) {
    const detail = detailFor(row.dataset.participant);
    if (!detail) return true;
    const field = detail.querySelector(spec.input);
    const msg = detail.querySelector(spec.msg);
    if (!field || !field.dataset.dirty) return true;
    const post = (confirm) => fetch(spec.url(), {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": window.csrfToken() },
      body: JSON.stringify({
        participant: row.dataset.participant,
        [spec.key]: field.value.trim(),
        confirm: confirm,
      }),
    }).then((r) => r.json()).catch(() => ({ ok: false, error: gettext("Could not save.") }));
    let res = await post(false);
    // A bib carries its recorded times with it, so the server refuses
    // a move like that until it has been said out loud and agreed to.
    if (!res.ok && res.confirm && spec.confirmTitle) {
      const agreed = await window.appConfirm({
        title: spec.confirmTitle(),
        body: res.confirm,
        accept: spec.confirmAccept(),
        danger: true,
      });
      if (!agreed) return false;
      res = await post(true);
    }
    if (res.ok) {
      delete field.dataset.dirty;
      field.classList.remove(spec.errorClass);
      if (msg) { msg.textContent = gettext("Saved"); msg.className = "pd-bib-msg pd-bib-msg--ok"; }
      const value = res[spec.key] == null ? "" : String(res[spec.key]);
      field.value = value;
      const display = row.querySelector(spec.display);
      if (display) display.textContent = value || "–";
      refreshSearch(row);
      if (spec.resort) resort();
      return true;
    }
    if (msg) {
      field.classList.add(spec.errorClass);
      msg.textContent = res.error || gettext("Could not save.");
      msg.className = "pd-bib-msg pd-bib-msg--error";
    }
    return false;
  }

  // Every field of a row, in panel order. Each is attempted even when an
  // earlier one was refused: a rejected bib must not silently swallow the
  // drawn number typed beside it.
  async function saveRow(row) {
    let ok = true;
    for (const spec of FIELDS) {
      if (!(await saveField(row, spec))) ok = false;
    }
    return ok;
  }

  // aria-expanded belongs to the button that does the expanding, not to the
  // row: a <tr role="button"> stops being a row for a screen reader, losing its
  // headers and its place in the table.
  function setExpanded(row, open) {
    const button = row.querySelector("[data-row-toggle]");
    if (button) button.setAttribute("aria-expanded", String(open));
  }

  function collapse(row) {
    if (!row) return;
    const detail = detailFor(row.dataset.participant);
    saveRow(row);
    setExpanded(row, false);
    row.classList.remove("participant-row--open");
    if (detail) detail.hidden = true;
    if (openRow === row) openRow = null;
  }

  function expand(row) {
    if (openRow && openRow !== row) collapse(openRow);
    const detail = detailFor(row.dataset.participant);
    setExpanded(row, true);
    row.classList.add("participant-row--open");
    if (detail) detail.hidden = false;
    openRow = row;
    // The first field the panel actually has: the drawn number while the event
    // draws them, which is what the desk types, and the bib otherwise.
    for (const spec of FIELDS) {
      const field = detail && detail.querySelector(spec.input);
      if (field) { field.focus(); field.select(); break; }
    }
  }

  function toggle(row) {
    if (openRow === row) collapse(row);
    else expand(row);
  }

  for (const row of rows) {
    row.addEventListener("click", (e) => {
      if (e.target.closest("a")) return;  // links do their own thing
      toggle(row);
    });
    // The button is a real button, so Enter and Space already work on it; the
    // row only needs the mouse handler above.
    const button = row.querySelector("[data-row-toggle]");
    if (button) button.addEventListener("click", (e) => { e.stopPropagation(); toggle(row); });
    const detail = detailFor(row.dataset.participant);
    let wired = false;
    for (const spec of FIELDS) {
      const field = detail && detail.querySelector(spec.input);
      if (!field) continue;
      wired = true;
      field.addEventListener("input", () => {
        field.dataset.dirty = "1";
        field.classList.remove(spec.errorClass);
        const msg = detail.querySelector(spec.msg);
        if (msg) msg.textContent = "";
      });
      field.addEventListener("blur", () => saveField(row, spec));
      // Enter saves the whole row; it only collapses once every field is
      // accepted, so a rejected bib (e.g. already in use) keeps the panel open
      // with its error visible.
      field.addEventListener("keydown", async (e) => {
        if (e.key !== "Enter") return;
        e.preventDefault();
        // Back to the row's toggle, which is now what holds the tab stop —
        // the row itself no longer has one (it is a row again, not a button).
        if (await saveRow(row)) {
          collapse(row);
          const toggleBtn = row.querySelector("[data-row-toggle]");
          if (toggleBtn) toggleBtn.focus();
        }
      });
    }
    // Clicks inside the detail shouldn't bubble to a row toggle.
    if (wired) detail.addEventListener("click", (e) => e.stopPropagation());
    wireDsq(row);
  }

  // The whole-event disqualification switch. It saves on the spot rather than
  // on collapse like the bib field: it is a single, consequential flag, and
  // the pill in the row is what confirms it landed.
  function wireDsq(row) {
    const detail = detailFor(row.dataset.participant);
    const box = detail && detail.querySelector("[data-dsq-input]");
    if (!box) return;
    const msg = detail.querySelector("[data-dsq-msg]");
    const pill = row.querySelector("[data-dsq-pill]");
    box.addEventListener("change", async () => {
      const wanted = box.checked;
      if (msg) { msg.textContent = ""; msg.className = "pd-dsq-msg"; }
      const res = await fetch(URLS.setDsq, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": window.csrfToken() },
        body: JSON.stringify({ participant: row.dataset.participant, dsq: wanted }),
      }).then((r) => r.json()).catch(() => ({ ok: false }));
      if (res.ok) {
        if (pill) pill.hidden = !wanted;
        if (msg) { msg.textContent = gettext("Saved"); msg.className = "pd-dsq-msg pd-dsq-msg--ok"; }
      } else {
        box.checked = !wanted;   // the server refused — don't show it as done
        if (msg) {
          msg.textContent = res.error || gettext("Could not save.");
          msg.className = "pd-dsq-msg pd-dsq-msg--error";
        }
      }
    });
  }

  // ---- live search filtering --------------------------------------------
  function apply() {
    const term = input.value.trim().toLowerCase();
    let visible = 0;
    for (const row of rows) {
      const match = term === "" || row.dataset.search.includes(term);
      row.hidden = !match;
      const detail = detailFor(row.dataset.participant);
      // A row filtered out collapses; a matching row keeps its open state.
      if (!match) {
        if (openRow === row) collapse(row);
        else if (detail) detail.hidden = true;
      }
      if (match) visible += 1;
    }
    if (noResults) noResults.hidden = !(rows.length > 0 && visible === 0);
  }

  // Clear (×) button: visible only when the field has text.
  const clearBtn = document.getElementById("participant-search-clear");
  function syncClear() { if (clearBtn) clearBtn.hidden = input.value === ""; }
  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      input.value = "";
      input.focus();
      apply();
      syncClear();
    });
  }

  input.addEventListener("input", () => { apply(); syncClear(); });
  document.getElementById("participant-search").addEventListener("submit", (e) => {
    e.preventDefault();
    apply();
  });
  apply();
  syncClear();
})();
