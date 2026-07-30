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

  // Order rows the way the server does: by bib ascending (unassigned last),
  // then last name, then first name. Called after a bib changes so the row
  // jumps to its correct place without a page reload.
  function bibKey(row) {
    const n = parseInt(row.querySelector("[data-bib-display]").textContent.trim(), 10);
    return Number.isNaN(n) ? Infinity : n;
  }
  function resort() {
    const ordered = rows.slice().sort((a, b) => {
      const ka = bibKey(a), kb = bibKey(b);
      if (ka !== kb) return ka - kb;
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

  // Persist the detail's bib field. Called on blur and before collapsing.
  // Returns true when there was nothing to save or the save succeeded, false
  // when the server rejected it (so callers can keep the panel open).
  async function saveBib(row) {
    const detail = detailFor(row.dataset.participant);
    if (!detail) return true;
    const field = detail.querySelector("[data-bib-input], .pd-bib-input");
    const msg = detail.querySelector("[data-bib-msg]");
    if (!field || !field.dataset.dirty) return true;
    const post = (confirm) => fetch(URLS.setBib, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": window.csrfToken() },
      body: JSON.stringify({
        participant: row.dataset.participant, bib: field.value.trim(), confirm: confirm,
      }),
    }).then((r) => r.json()).catch(() => ({ ok: false, error: gettext("Could not save.") }));
    let res = await post(false);
    // A bib carries its recorded times with it (DAT-2), so the server refuses
    // a move like that until it has been said out loud and agreed to.
    if (!res.ok && res.confirm) {
      const agreed = await window.appConfirm({
        title: gettext("Change the bib?"),
        body: res.confirm,
        accept: gettext("Change the bib"),
        danger: true,
      });
      if (!agreed) return false;
      res = await post(true);
    }
    if (res.ok) {
      delete field.dataset.dirty;
      field.classList.remove("pd-bib-input--error");
      if (msg) { msg.textContent = gettext("Saved"); msg.className = "pd-bib-msg pd-bib-msg--ok"; }
      const bib = res.bib == null ? "" : String(res.bib);
      field.value = bib;
      const display = row.querySelector("[data-bib-display]");
      if (display) display.textContent = bib || "–";
      row.dataset.search = row.dataset.searchBase + " " + bib;
      resort();
      return true;
    }
    if (msg) {
      field.classList.add("pd-bib-input--error");
      msg.textContent = res.error || gettext("Could not save.");
      msg.className = "pd-bib-msg pd-bib-msg--error";
    }
    return false;
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
    saveBib(row);
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
    const field = detail && detail.querySelector(".pd-bib-input");
    if (field) { field.focus(); field.select(); }
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
    const field = detail && detail.querySelector(".pd-bib-input");
    if (field) {
      field.addEventListener("input", () => {
        field.dataset.dirty = "1";
        field.classList.remove("pd-bib-input--error");
        const msg = detail.querySelector("[data-bib-msg]");
        if (msg) msg.textContent = "";
      });
      field.addEventListener("blur", () => saveBib(row));
      // Enter saves; it only collapses once the save succeeds, so a rejected
      // bib (e.g. already in use) keeps the panel open with its error visible.
      field.addEventListener("keydown", async (e) => {
        if (e.key !== "Enter") return;
        e.preventDefault();
        // Back to the row's toggle, which is now what holds the tab stop —
        // the row itself no longer has one (it is a row again, not a button).
        if (await saveBib(row)) {
          collapse(row);
          const toggleBtn = row.querySelector("[data-row-toggle]");
          if (toggleBtn) toggleBtn.focus();
        }
      });
      // Clicks inside the detail shouldn't bubble to a row toggle.
      detail.addEventListener("click", (e) => e.stopPropagation());
    }
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
