/* Competition Setup > Results: the parts of the page that are not the PDF editor
(which is static/js/results_pdf_editor.js).
 */

(function () {
  // A class can only *add* columns General doesn't already show: hide each
  // class checkbox whose General counterpart is on, live as General changes.
  const generals = document.querySelectorAll("[data-general-key]");
  if (!generals.length) return;
  const apply = () => {
    const on = new Set();
    generals.forEach((g) => { if (g.checked) on.add(g.dataset.generalKey); });
    document.querySelectorAll("[data-class-block]").forEach((block) => {
      let visible = 0;
      block.querySelectorAll("[data-col-key]").forEach((label) => {
        const hide = on.has(label.dataset.colKey);
        label.hidden = hide;
        if (!hide) visible += 1;
      });
      const note = block.querySelector(".settings-empty-note");
      if (note) note.hidden = visible !== 0;
    });
  };
  generals.forEach((g) => g.addEventListener("change", apply));
  apply();
})();
