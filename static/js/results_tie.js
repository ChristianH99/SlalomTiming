// Inline manual tie-break editor for the results tables.
//
// Each tied competitor's rank cell carries a flag (red = unresolved, green =
// resolved). Clicking a flag opens edit mode for that whole tie group: its rows
// become draggable to reorder and each rank turns into a number input. Saving
// posts the order + ranks to results:tie-resolve, which validates that it's a
// legal ranking (start position fixed; each rank ties the previous or takes its
// own position — 1,2 or 1,1 but never 2,1) and stores it. The page reloads to
// show the result (green flag).
(function () {
  const editor = document.getElementById("tie-editor");
  const toolbar = document.getElementById("tie-toolbar");
  if (!editor || !toolbar) return;

  const scope = editor.dataset.scope;
  const resolveUrl = editor.dataset.resolveUrl;
  const csrf = editor.querySelector("[name=csrfmiddlewaretoken]");
  const msg = document.getElementById("tie-toolbar-msg");

  let active = null;   // { group, rows: [{tr, originalCell}] }
  let dragging = null;

  function rowsFor(group) {
    return [...document.querySelectorAll(`tr[data-tie-group="${cssEscape(group)}"]`)];
  }

  function cssEscape(value) {
    return window.CSS && CSS.escape ? CSS.escape(value) : value.replace(/"/g, '\\"');
  }

  function open(group) {
    if (active) cancel();
    const trs = rowsFor(group);
    if (!trs.length) return;
    const start = parseInt(trs[0].dataset.tieStart, 10) || 1;
    active = { group, rows: [], start };
    trs.forEach((tr) => {
      const cell = tr.querySelector("[data-rank-cell]");
      const original = cell.innerHTML;
      const rank = tr.dataset.rank || start;
      cell.innerHTML =
        `<span class="tie-drag" title="Drag to reorder">⠿</span>` +
        `<input type="number" class="tie-rank-input" value="${rank}" ` +
        `min="${start}" max="${start + trs.length - 1}" inputmode="numeric">`;
      tr.classList.add("tie-editing");
      tr.draggable = true;
      tr.addEventListener("dragstart", onDragStart);
      tr.addEventListener("dragover", onDragOver);
      tr.addEventListener("dragend", onDragEnd);
      active.rows.push({ tr, cell, original });
    });
    setMessage("Drag the tied competitors into order, then set their ranks.", false);
    toolbar.hidden = false;
  }

  function cancel() {
    if (!active) return;
    active.rows.forEach(({ tr, cell, original }) => {
      cell.innerHTML = original;
      tr.classList.remove("tie-editing", "tie-drop-before", "tie-drop-after");
      tr.draggable = false;
      tr.removeEventListener("dragstart", onDragStart);
      tr.removeEventListener("dragover", onDragOver);
      tr.removeEventListener("dragend", onDragEnd);
    });
    active = null;
    toolbar.hidden = true;
  }

  function onDragStart(event) {
    dragging = event.currentTarget;
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", "");
  }

  function onDragOver(event) {
    if (!dragging) return;
    const target = event.currentTarget;
    if (target === dragging || target.dataset.tieGroup !== active.group) return;
    event.preventDefault();
    const rect = target.getBoundingClientRect();
    const after = event.clientY > rect.top + rect.height / 2;
    target.parentNode.insertBefore(dragging, after ? target.nextSibling : target);
  }

  function onDragEnd() {
    dragging = null;
  }

  function setMessage(text, isError) {
    msg.textContent = text;
    msg.classList.toggle("tie-toolbar-msg--error", !!isError);
  }

  // Client-side mirror of the server rule, for a friendly message before posting.
  function validate(members, start) {
    let previous = null;
    for (let i = 0; i < members.length; i += 1) {
      const rank = members[i].rank;
      if (Number.isNaN(rank)) return "Every rank needs a number.";
      if (i === 0) {
        if (rank !== start) return `The first competitor must be rank ${start}.`;
      } else if (rank !== previous && rank !== start + i) {
        return "Ranks must go in order — a tie keeps the lower number.";
      }
      previous = rank;
    }
    return null;
  }

  function save() {
    if (!active) return;
    const rows = rowsFor(active.group);  // current DOM order
    const members = rows.map((tr) => ({
      entry_pk: parseInt(tr.dataset.entry, 10),
      occurrence: parseInt(tr.dataset.occ, 10),
      rank: parseInt(tr.querySelector(".tie-rank-input").value, 10),
    }));
    const error = validate(members, active.start);
    if (error) { setMessage(error, true); return; }

    fetch(resolveUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf.value },
      body: JSON.stringify({ scope, members }),
    })
      .then((response) => response.json().then((data) => ({ ok: response.ok, data })))
      .then(({ ok, data }) => {
        if (ok && data.ok) {
          window.location.reload();
        } else {
          setMessage(data.error || "Could not save.", true);
        }
      })
      .catch(() => setMessage("Could not save.", true));
  }

  document.addEventListener("click", (event) => {
    const flag = event.target.closest("[data-tie-flag]");
    if (flag) {
      const tr = flag.closest("tr[data-tie-group]");
      if (tr) open(tr.dataset.tieGroup);
    }
  });
  document.getElementById("tie-save").addEventListener("click", save);
  document.getElementById("tie-cancel").addEventListener("click", cancel);
})();
