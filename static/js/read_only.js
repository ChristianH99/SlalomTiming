/* A signed-off event is looked at, not driven.
 *
 * The server already refuses every write for an archived competition
 * (apps/competitions/archiving.py). This is the other half: the *page* has to
 * stop offering them. A penalty stepper that accepts a click, sends it, and has
 * it turned away is worse than one that is visibly dead — the operator learns
 * nothing from a number that snaps back, and on the timing views several of
 * these controls are how the event was run all day.
 *
 * Two things make this a sweep rather than a per-view change:
 *
 *   * The four live views re-render themselves from JSON on every WebSocket
 *     nudge, so anything disabled once is replaced seconds later. A
 *     MutationObserver catches every control the moment it is added, whichever
 *     render pass added it, including ones written after this file.
 *   * It is blanket-then-allow, not a list of what to disable. A control added
 *     next year is off by default; the cost of getting that wrong is a search
 *     box somebody notices in a minute, against a live stepper on a finished
 *     event that nobody notices at all.
 *
 * Opt out with `data-read-only-allow` on the control or any ancestor: reads
 * (search, expanding a row) and anything that leaves the event alone.
 * PDF export needs no marker — every export control is a link.
 */
(function () {
  if (!document.body.hasAttribute("data-read-only")) return;

  const CONTROLS = "input, select, textarea, button";

  function freeze(element) {
    if (element.closest("[data-read-only-allow]")) return;
    if (element.matches(CONTROLS)) {
      element.disabled = true;
      // A disabled control is skipped by the tab order, which is the correct
      // reading of a page that cannot be operated.
      element.setAttribute("aria-disabled", "true");
    }
    if (element.getAttribute("draggable") === "true") {
      element.setAttribute("draggable", "false");
    }
  }

  function freezeAll(root) {
    if (root.nodeType !== 1) return;
    freeze(root);
    root.querySelectorAll(CONTROLS + ", [draggable='true']").forEach(freeze);
  }

  const main = document.getElementById("main-content");
  if (!main) return;

  freezeAll(main);

  new MutationObserver((records) => {
    records.forEach((record) => {
      record.addedNodes.forEach(freezeAll);
    });
  }).observe(main, { childList: true, subtree: true });

  // Both timing views move times by dragging rather than by a control, so the
  // observer above has nothing to disable — the drag is a listener on a chip.
  // Refusing the gesture itself is the only place to stop it.
  main.addEventListener("dragstart", (event) => {
    if (event.target.closest && event.target.closest("[data-read-only-allow]")) return;
    event.preventDefault();
  }, true);
})();
