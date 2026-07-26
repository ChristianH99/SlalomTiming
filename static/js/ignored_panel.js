/* The Ignored-times rail, shared by Manual timing and Auto timing.
 *
 * Both views render the same panel (templates/timing/_ignored.html) and used to
 * carry their own near-identical copy of this code, differing only in which drag
 * handler they wired up. It lives here now so the rail can't drift between the
 * two pages, the same way live_socket.js owns the WebSocket for all four live
 * views.
 *
 * Three things it does that the old copies didn't:
 *
 *  - Splits into Start / Finish columns only when the rig actually has two
 *    channels (`ignored_split`). With a single light barrier a signal's role
 *    depends on what the arrangement was doing when it arrived, so an *ignored*
 *    signal has none — the old panel asked `signal.role()` anyway, got START for
 *    every chip, and rendered a Finish column that was always empty.
 *  - Shows how old each chip is. A chip's own time is the *device's* clock,
 *    which on a real rig is not wall-clock, so on its own it says nothing about
 *    when the signal turned up.
 *  - Caps the list and counts the rest behind a "show all", because after a
 *    morning of practice runs this is otherwise dozens of chips deep.
 *
 * There is deliberately no "clear all": an ignored signal is still the only
 * record that the device fired, and this app does not delete recorded times.
 */
window.IgnoredPanel = (function () {
  "use strict";

  var VISIBLE = 6; // chips per column before the list folds

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  // "just now" / "3 min" / "2 h" — coarse on purpose. The operator wants to know
  // whether a chip is from this run or from before lunch, not the second.
  function age(iso) {
    if (!iso) return "";
    var then = Date.parse(iso);
    if (isNaN(then)) return "";
    var mins = Math.floor((Date.now() - then) / 60000);
    if (mins < 1) return gettext("just now");
    if (mins < 60) return interpolate(gettext("%(n)s min"), { n: mins }, true);
    return interpolate(gettext("%(n)s h"), { n: Math.floor(mins / 60) }, true);
  }

  /* opts: { box, countEl, moreEl, onDragStart(event, id, role, time),
   *         onDragEnd(), onRestore(id) } */
  function create(opts) {
    var expanded = false;

    function chip(sig) {
      var node = el("div", "ignored-chip" + (sig.manual ? " ignored-chip--manual" : ""));
      node.append(el("span", "ignored-chip-time", sig.time));
      var when = age(sig.received_at);
      if (when) node.append(el("span", "ignored-chip-age", when));
      node.draggable = true;
      node.dataset.signalId = sig.id;
      node.dataset.role = sig.role;
      node.dataset.time = sig.time;
      node.title = gettext("Drag onto a run's slot · double-click to restore");
      node.addEventListener("dragstart", function (e) {
        opts.onDragStart(e, sig.id, sig.role, sig.time);
      });
      node.addEventListener("dragend", opts.onDragEnd);
      node.addEventListener("dblclick", function () { opts.onRestore(sig.id); });
      return node;
    }

    function render(signals, split) {
      var all = signals || [];
      opts.box.classList.toggle("ignored-box--single", !split);

      opts.box.querySelectorAll(".ignored-col").forEach(function (col) {
        var role = col.dataset.role;
        // Unsplit: everything goes in the start column and the finish column is
        // removed from the flow entirely (not just left empty).
        var mine = split ? all.filter(function (s) { return s.role === role; })
                         : (role === "start" ? all : []);
        col.hidden = !split && role !== "start";
        col.classList.toggle("ignored-col--empty", mine.length === 0);
        var shown = expanded ? mine : mine.slice(0, VISIBLE);
        col.querySelector(".ignored-col-list").replaceChildren.apply(
          col.querySelector(".ignored-col-list"), shown.map(chip));
      });

      if (opts.countEl) {
        opts.countEl.textContent = all.length;
        opts.countEl.hidden = all.length === 0;
      }
      if (opts.moreEl) {
        // The fold is per column, so the button appears as soon as any column
        // has more than it is showing.
        var most = split
          ? Math.max.apply(null, ["start", "finish"].map(function (r) {
              return all.filter(function (s) { return s.role === r; }).length;
            }))
          : all.length;
        var hidden = most - VISIBLE;
        opts.moreEl.hidden = hidden <= 0;
        opts.moreEl.textContent = expanded
          ? gettext("Show fewer")
          : interpolate(gettext("Show all (%(n)s)"), { n: all.length }, true);
        opts.moreEl.onclick = function () {
          expanded = !expanded;
          render(signals, split);
        };
      }
    }

    return { render: render };
  }

  return { create: create };
})();
