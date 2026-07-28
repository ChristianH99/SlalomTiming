/* The competition-type list: its search filter.
 */

(function () {
  document.querySelectorAll("[data-expander]").forEach((button) => {
    button.addEventListener("click", () => {
      const row = button.closest("tr");
      const panel = row.nextElementSibling;
      if (!panel || !panel.matches("[data-type-competitions]")) return;
      const opening = panel.hasAttribute("hidden");
      panel.toggleAttribute("hidden", !opening);
      button.setAttribute("aria-expanded", String(opening));
      button.innerHTML = opening ? "&#9660;" : "&#9654;";
    });
  });
})();
