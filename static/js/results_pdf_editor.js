// Rich-text editors + wildcard inserter for the results-PDF layout on the
// Results settings page. Two small contenteditable boxes (header and footer) let
// the operator set bold / text size and drop in wildcards; their HTML is synced
// into hidden inputs on save, and an "Export sample" button posts the current
// (unsaved) settings to build a preview PDF.
(function () {
  "use strict";
  const root = document.querySelector(".pdf-layout");
  if (!root) return;

  // Prefer HTML tags (<b>) over inline styles so the server sanitiser keeps them.
  try { document.execCommand("styleWithCSS", false, false); } catch (e) { /* ignore */ }

  const editors = Array.from(root.querySelectorAll(".pdf-editor"));
  let lastEditor = editors[0] || null;

  editors.forEach((ed) => {
    ed.addEventListener("focus", () => { lastEditor = ed; });
  });

  function fireInput(ed) {
    ed.dispatchEvent(new Event("input", { bubbles: true }));
  }

  // --- toolbar: bold + text size ---
  root.querySelectorAll(".pdf-toolbar").forEach((bar) => {
    const editor = document.getElementById(bar.dataset.editor);
    bar.querySelectorAll(".pdf-tool").forEach((btn) => {
      // Keep the editor selection alive when the button takes focus.
      btn.addEventListener("mousedown", (e) => e.preventDefault());
      btn.addEventListener("click", () => {
        editor.focus();
        if (btn.dataset.cmd === "bold") {
          document.execCommand("bold");
        } else if (btn.dataset.size) {
          applySize(editor, btn.dataset.size);
        }
        fireInput(editor);
      });
    });
  });

  function applySize(editor, cls) {
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount) return;
    const range = sel.getRangeAt(0);
    if (range.collapsed || !editor.contains(range.commonAncestorContainer)) return;
    const span = document.createElement("span");
    span.className = cls;
    try {
      span.appendChild(range.extractContents());
      range.insertNode(span);
    } catch (e) {
      return;
    }
    sel.removeAllRanges();
    const r = document.createRange();
    r.selectNodeContents(span);
    sel.addRange(r);
  }

  // --- wildcard chips ---
  root.querySelectorAll(".pdf-chip").forEach((chip) => {
    chip.addEventListener("mousedown", (e) => e.preventDefault());
    chip.addEventListener("click", () => {
      const editor = lastEditor || editors[0];
      if (!editor) return;
      insertToken(editor, chip.dataset.token);
    });
  });

  function insertToken(editor, token) {
    editor.focus();
    const sel = window.getSelection();
    if (sel && sel.rangeCount && editor.contains(sel.anchorNode)) {
      const range = sel.getRangeAt(0);
      range.deleteContents();
      const node = document.createTextNode(token);
      range.insertNode(node);
      range.setStartAfter(node);
      range.collapse(true);
      sel.removeAllRanges();
      sel.addRange(range);
    } else {
      editor.appendChild(document.createTextNode(token));
    }
    fireInput(editor);
  }

  // --- live #increment value as the start year changes ---
  const yearInput = document.getElementById("increment_start_year");
  const compYear = parseInt(root.dataset.competitionYear, 10);
  const incValue = root.querySelector('.pdf-value[data-token="#increment"]');
  function updateIncrement() {
    if (!incValue) return;
    const start = parseInt(yearInput && yearInput.value, 10);
    if (!start || !compYear || compYear < start) {
      incValue.textContent = "—";
    } else {
      incValue.textContent = String(compYear - start + 1);
    }
  }
  if (yearInput) yearInput.addEventListener("input", updateIncrement);
  updateIncrement();

  // --- sync editors into their hidden inputs ---
  function syncEditors() {
    editors.forEach((ed) => {
      const input = document.getElementById(ed.dataset.target);
      if (input) input.value = ed.innerHTML;
    });
  }
  const form = document.getElementById("results-form");
  if (form) form.addEventListener("submit", syncEditors, true);

  // --- one-click logo removal ---
  const logoGrid = root.querySelector(".pdf-logo-grid");
  const csrf = () => {
    const el = document.querySelector('#results-form [name="csrfmiddlewaretoken"]');
    return el ? el.value : "";
  };
  if (logoGrid) {
    logoGrid.querySelectorAll(".pdf-logo-remove").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const side = btn.dataset.side;
        btn.disabled = true;
        const body = new FormData();
        body.append("side", side);
        body.append("csrfmiddlewaretoken", csrf());
        try {
          const res = await fetch(logoGrid.dataset.removeUrl, { method: "POST", body });
          if (!res.ok) throw new Error("status " + res.status);
          const cell = logoGrid.querySelector('.pdf-logo[data-side="' + side + '"]');
          const wrap = cell.querySelector(".pdf-logo-preview-wrap");
          if (wrap) wrap.hidden = true;
          const file = cell.querySelector('input[type="file"]');
          if (file) file.value = "";
        } catch (e) {
          window.appAlert({
            title: gettext("Could not remove the logo"),
            body: e.message,
          });
          btn.disabled = false;
        }
      });
    });
  }

  // --- sample export: post current settings, open the returned PDF ---
  const sampleBtn = document.getElementById("pdf-sample-btn");
  if (sampleBtn && form) {
    sampleBtn.addEventListener("click", async () => {
      syncEditors();
      sampleBtn.disabled = true;
      const original = sampleBtn.textContent;
      sampleBtn.textContent = gettext("Building…");
      try {
        const res = await fetch(root.dataset.sampleUrl, {
          method: "POST",
          body: new FormData(form),  // carries csrfmiddlewaretoken + current fields
        });
        if (!res.ok) throw new Error("status " + res.status);
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        window.open(url, "_blank");
        setTimeout(() => URL.revokeObjectURL(url), 60000);
      } catch (e) {
        window.appAlert({
          title: gettext("Could not build the sample PDF"),
          body: e.message,
        });
      } finally {
        sampleBtn.disabled = false;
        sampleBtn.textContent = original;
      }
    });
  }
})();
