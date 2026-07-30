/* The app shell — what base.html carries on every page: the modal machinery
 * (focus management, and the app's own confirm/alert), the unsaved-changes
 * guard, data-confirm forms, the page help pop-up, the language selector, the
 * sidebar toggle and the dismissable messages.
 *
 * They were five inline <script> blocks until the app grew a
 * Content-Security-Policy. A CSP has no way to allow *our* inline script without
 * also allowing injected inline script, which is the whole thing it exists to
 * stop — so nothing on a page may be inline any more, and every one of these
 * moved out as it stood. See config/settings.py.
 */

// Page data handed over by the template through `json_script` (the app ships a
// Content-Security-Policy, so nothing may be written into an inline <script>).
// Reads defensively: a missing element means the page did not render its data —
// with `getElementById(...).textContent` that is a TypeError thrown before the
// script's own "nothing to do here" guard ever runs, which turns a page that
// should quietly do nothing into one with a broken script.
window.pageData = function pageData(id) {
  const el = document.getElementById(id);
  if (!el) return null;
  try {
    return JSON.parse(el.textContent);
  } catch (err) {
    return null;
  }
};

// The CSRF token, for the pages that POST with fetch().
//
// Read from the cookie rather than from a token the template rendered into the
// page: a file cannot be handed a template variable, and settings.py already
// keeps CSRF_COOKIE_HTTPONLY false for exactly this. Called at use time, not at
// load time — the cookie is rotated on login, and a page open across one would
// otherwise hold a stale token for as long as it stayed open.
window.csrfToken = function csrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]*)/);
  return match ? decodeURIComponent(match[1]) : "";
};

/* ---- modals -------------------------------------------------------------
 *
 * Every dialog in this app goes through window.modalController. Before it, each
 * one hid and showed its own overlay, which looked right and behaved wrongly:
 * `aria-modal="true"` was on the markup but nothing moved focus into the dialog,
 * kept it there, or gave it back afterwards. Tab went straight through to the
 * page behind the overlay — so a keyboard user could type into a form they could
 * not see, and the button they had just pressed lost its place for good.
 */
const FOCUSABLE = [
  "a[href]", "button:not([disabled])", "input:not([disabled])",
  "select:not([disabled])", "textarea:not([disabled])", "[tabindex]",
].join(",");

window.modalController = function modalController(modal, options) {
  const opts = options || {};
  let lastFocused = null;

  const focusable = () =>
    Array.from(modal.querySelectorAll(FOCUSABLE))
      .filter((el) => el.tabIndex !== -1 && el.offsetParent !== null);

  function focusIn() {
    // The first control, or the dialog itself when it holds none (help text).
    const first = focusable()[0];
    if (first) {
      first.focus();
    } else {
      const panel = modal.querySelector(".modal") || modal;
      panel.tabIndex = -1;
      panel.focus();
    }
  }

  function open() {
    if (!modal.hidden) return;
    lastFocused = document.activeElement;
    modal.hidden = false;
    document.body.classList.add("modal-open");
    focusIn();
  }

  function close() {
    if (modal.hidden) return;
    modal.hidden = true;
    document.body.classList.remove("modal-open");
    // Back where they were. A dialog opened from a table row is the case that
    // matters: without this, focus resets to the top of the document and the
    // row they were working on is however many tabs away.
    if (lastFocused && document.contains(lastFocused)) lastFocused.focus();
    lastFocused = null;
    if (opts.onClose) opts.onClose();
  }

  modal.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      close();
      return;
    }
    if (event.key !== "Tab") return;
    const items = focusable();
    if (!items.length) { event.preventDefault(); return; }
    const first = items[0];
    const last = items[items.length - 1];
    // Wrap, rather than letting Tab walk out of the dialog and behind it.
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  if (opts.backdropCloses !== false) {
    modal.addEventListener("click", (event) => {
      if (event.target === modal) close();
    });
  }

  // Some dialogs are rendered *already open* by the server — a form comes back
  // with "this would delete 14 penalties, are you sure?" on it. Those never went
  // through open(), so they would otherwise be the one kind of modal with no
  // focus in it at all, which is the worst case: the operator is being asked a
  // question and their keyboard is somewhere behind the overlay.
  if (!modal.hidden) {
    document.body.classList.add("modal-open");
    focusIn();
  }

  return { open, close, element: modal, isOpen: () => !modal.hidden };
};

/* The app's own confirm() and alert(), in the page's own styling and language.
 *
 * The browser's are deliberately not used anywhere (base.html says why). Both
 * return a promise, so a caller reads the same way the built-ins did:
 *
 *     if (!(await window.appConfirm({ body: "…" }))) return;
 */
(function () {
  const modal = document.querySelector("[data-app-dialog]");
  if (!modal) return;
  const title = modal.querySelector("[data-dialog-title]");
  const body = modal.querySelector("[data-dialog-body]");
  const accept = modal.querySelector("[data-dialog-accept]");
  const cancel = modal.querySelector("[data-dialog-cancel]");
  let settle = null;

  const finish = (answer) => {
    const resolve = settle;
    settle = null;
    controller.close();
    if (resolve) resolve(answer);
  };

  // Closing by Escape or the backdrop is a "no", the same as Cancel.
  const controller = window.modalController(modal, { onClose: () => {
    if (settle) { const resolve = settle; settle = null; resolve(false); }
  } });

  accept.addEventListener("click", () => finish(true));
  cancel.addEventListener("click", () => finish(false));

  function show(config) {
    title.textContent = config.title || "";
    title.hidden = !config.title;
    // Each paragraph its own <p>: the built-in dialogs took one string with
    // newlines in it, which is why every caller was writing "\n\n".
    body.textContent = "";
    String(config.body || "").split("\n").forEach((line) => {
      if (!line.trim()) return;
      const p = document.createElement("p");
      p.textContent = line;
      body.appendChild(p);
    });
    accept.textContent = config.accept || gettext("OK");
    accept.className = config.danger ? "button button--danger" : "button";
    cancel.hidden = !config.cancellable;
    return new Promise((resolve) => { settle = resolve; controller.open(); });
  }

  window.appConfirm = (config) =>
    show(Object.assign({ cancellable: true, accept: gettext("Continue") }, config));
  window.appAlert = (config) =>
    show(Object.assign({ cancellable: false, accept: gettext("OK") }, config)).then(() => undefined);
})();

// Guard against leaving a page with unsaved form changes. Opt in by putting
// data-unsaved-guard on the form; pages fire an "unsaved-change" event for
// mutations that aren't plain input/change (drag, add/remove rows, set-all).
(function () {
  const form = document.querySelector("form[data-unsaved-guard]");
  const modal = document.getElementById("unsaved-modal");
  if (!form || !modal) return;

  let dirty = false;
  let bypass = false;      // set once we intend to navigate (save/discard/submit)
  let pendingUrl = null;

  // Controls inside a data-no-dirty region don't edit what gets saved (preview
  // and other view-only switches), so they must not count as a change.
  const markDirty = (event) => {
    const target = event && event.target;
    if (target && target.closest && target.closest("[data-no-dirty]")) return;
    dirty = true;
  };
  form.addEventListener("input", markDirty);
  form.addEventListener("change", markDirty);
  document.addEventListener("unsaved-change", markDirty);
  form.addEventListener("submit", () => { bypass = true; });

  const controller = window.modalController(modal, { onClose: () => { pendingUrl = null; } });

  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[href]");
    if (!link || bypass || !dirty) return;
    if (link.target === "_blank" || link.hasAttribute("download")) return;
    const href = link.getAttribute("href");
    if (!href || href.startsWith("#") || href.startsWith("mailto:")) return;
    const dest = new URL(link.href, window.location.href);
    if (dest.origin !== window.location.origin) return;  // only guard in-app nav
    event.preventDefault();
    pendingUrl = link.href;
    controller.open();
  });

  /* Closing the tab, and the Back button.
   *
   * The in-page dialog above can only catch a click on a link we render. A tab
   * being closed is the one case a page cannot draw its own dialog for — the
   * browser's is the only thing that can stop it, and it is deliberately not
   * ours to style or word (browsers ignore the string, and have for years).
   *
   * Back is a different problem: `beforeunload` does not fire reliably for it,
   * so a history entry is pushed while the form is dirty and popped here. The
   * entry is put back before asking, so saying "Cancel" leaves the operator on
   * the page they were editing rather than one step further back in history.
   */
  window.addEventListener("beforeunload", (event) => {
    if (!dirty || bypass) return;
    event.preventDefault();
    event.returnValue = "";      // the incantation Chrome and Safari still want
  });

  let guardingHistory = false;
  const guardHistory = () => {
    if (guardingHistory) return;
    guardingHistory = true;
    history.pushState({ unsavedGuard: true }, "", window.location.href);
  };
  form.addEventListener("input", guardHistory);
  form.addEventListener("change", guardHistory);
  document.addEventListener("unsaved-change", guardHistory);

  window.addEventListener("popstate", () => {
    if (!dirty || bypass) return;
    guardingHistory = false;
    guardHistory();
    pendingUrl = "";             // "back", rather than a URL we can navigate to
    controller.open();
  });

  const leave = () => {
    bypass = true;
    const url = pendingUrl;
    controller.close();
    if (url) {
      window.location.href = url;
    } else {
      history.go(-2);            // past our own guard entry, to where Back meant
    }
  };

  modal.querySelector("[data-unsaved-cancel]").addEventListener("click", () => controller.close());
  modal.querySelector("[data-unsaved-discard]").addEventListener("click", leave);
  modal.querySelector("[data-unsaved-save]").addEventListener("click", () => {
    bypass = true;
    const next = form.querySelector('input[name="next"]');
    if (next && pendingUrl) next.value = pendingUrl;  // return here after saving
    controller.close();
    // requestSubmit, not submit: form.submit() skips HTML5 constraint validation
    // *and* every submit listener, so "Save changes" posted a form the Save
    // button itself would have refused — and the page's own submit handlers
    // (which is where several pages serialise their state) never ran.
    if (form.requestSubmit) form.requestSubmit();
    else form.submit();
  });
})();

// Page help: any [data-help-open] button opens the page's [data-help-modal];
// click the ×/backdrop or press Escape to close. A page-internal pop-up.
(function () {
  const openers = document.querySelectorAll("[data-help-open]");
  const modal = document.querySelector("[data-help-modal]");
  if (!openers.length || !modal) return;
  const controller = window.modalController(modal);
  openers.forEach((btn) => btn.addEventListener("click", controller.open));
  modal.addEventListener("click", (event) => {
    if (event.target.closest("[data-help-close]")) controller.close();
  });
})();

// Any form carrying data-confirm asks before it submits — through the app's own
// dialog, not the browser's. Replaces the inline onsubmit="return confirm(...)"
// attributes, which a Content-Security-Policy has no way to allow.
//
// The answer arrives a turn later than window.confirm's did, so the submit is
// always stopped and re-issued once the operator has said yes. `agreed` is what
// stops the second pass asking again.
(function () {
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    let agreed = false;
    form.addEventListener("submit", async (event) => {
      if (agreed) return;
      event.preventDefault();
      const ok = await window.appConfirm({
        body: form.dataset.confirm,
        accept: gettext("Continue"),
        danger: form.matches("[data-confirm-danger]"),
      });
      if (!ok) return;
      agreed = true;
      if (form.requestSubmit) form.requestSubmit();
      else form.submit();
    });
  });
})();

// Topbar language selector: toggle the menu; close on outside click or Escape.
(function () {
  const root = document.querySelector("[data-lang-switcher]");
  if (!root) return;
  const toggle = root.querySelector("[data-lang-toggle]");
  const menu = root.querySelector("[data-lang-menu]");
  if (!toggle || !menu) return;

  const open = () => { menu.hidden = false; toggle.setAttribute("aria-expanded", "true"); };
  const close = () => { menu.hidden = true; toggle.setAttribute("aria-expanded", "false"); };

  toggle.addEventListener("click", (event) => {
    event.stopPropagation();
    menu.hidden ? open() : close();
  });
  document.addEventListener("click", (event) => {
    if (!menu.hidden && !root.contains(event.target)) close();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) close();
  });
})();

(function () {
  const shell = document.querySelector(".shell");
  const overlay = document.getElementById("shell-overlay");
  const toggle = document.getElementById("shell-hamburger");
  if (!shell || !overlay || !toggle) return;

  const isMobile = () => window.matchMedia("(max-width: 900px)").matches;
  const STORE = "slalom.sidebar-collapsed";

  // Collapsing is a preference about the whole app, not about one page, and it
  // used to reset on every navigation — so an operator who wanted the width for
  // the timing table had to collapse it again on each page they opened.
  // Desktop only: on mobile the sidebar is off-canvas anyway and opening it is
  // a momentary thing.
  let stored = null;
  try { stored = window.localStorage.getItem(STORE); } catch (err) { stored = null; }
  if (stored === "1") shell.classList.add("sidebar-collapsed");

  // The sidebar is shown by default on desktop and hidden (off-canvas) on
  // mobile, so "expanded" is derived differently per viewport. The attribute is
  // also *rendered* wrong — the template cannot know the viewport — which is
  // why this runs once at load and not only on click.
  function syncAria() {
    const shown = isMobile()
      ? shell.classList.contains("sidebar-open")
      : !shell.classList.contains("sidebar-collapsed");
    toggle.setAttribute("aria-expanded", String(shown));
  }

  toggle.addEventListener("click", () => {
    // Mobile slides the sidebar in; desktop collapses it out of the way.
    if (isMobile()) {
      shell.classList.toggle("sidebar-open");
    } else {
      const collapsed = shell.classList.toggle("sidebar-collapsed");
      try { window.localStorage.setItem(STORE, collapsed ? "1" : "0"); } catch (err) { /* private mode */ }
    }
    syncAria();
  });
  overlay.addEventListener("click", () => {
    shell.classList.remove("sidebar-open");
    syncAria();
  });
  window.addEventListener("resize", syncAria);
  syncAria();
})();

/* Django's messages — "Timing settings saved", "Could not delete the class".
 *
 * They were a plain <ul>, so a screen reader was never told a thing had
 * happened: the page simply had one more list on it than before. role="status"
 * announces politely (after whatever is being read now, which is right for a
 * confirmation), and each one gets a dismiss button — they used to sit there
 * until the next navigation, above the very form the operator was working in.
 */
(function () {
  document.querySelectorAll(".messages .notice").forEach((notice) => {
    const close = document.createElement("button");
    close.type = "button";
    close.className = "notice-dismiss";
    close.setAttribute("aria-label", gettext("Dismiss"));
    close.textContent = "×";
    close.addEventListener("click", () => notice.remove());
    notice.appendChild(close);
  });
})();
