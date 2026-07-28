/* The app shell — the five behaviours base.html carries on every page: the
 * unsaved-changes guard, data-confirm forms, the page help pop-up, the language
 * selector and the sidebar toggle.
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

  function openModal() { modal.hidden = false; document.body.classList.add("modal-open"); }
  function closeModal() { modal.hidden = true; document.body.classList.remove("modal-open"); pendingUrl = null; }

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
    openModal();
  });

  modal.querySelector("[data-unsaved-cancel]").addEventListener("click", closeModal);
  modal.addEventListener("click", (event) => { if (event.target === modal) closeModal(); });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !modal.hidden) closeModal();
  });
  modal.querySelector("[data-unsaved-discard]").addEventListener("click", () => {
    const url = pendingUrl;
    bypass = true; closeModal();
    if (url) window.location.href = url;
  });
  modal.querySelector("[data-unsaved-save]").addEventListener("click", () => {
    bypass = true;
    const next = form.querySelector('input[name="next"]');
    if (next && pendingUrl) next.value = pendingUrl;  // return here after saving
    form.submit();
  });
})();

// Page help: any [data-help-open] button opens the page's [data-help-modal];
// click the ×/backdrop or press Escape to close. A page-internal pop-up.
(function () {
  const openers = document.querySelectorAll("[data-help-open]");
  const modal = document.querySelector("[data-help-modal]");
  if (!openers.length || !modal) return;
  const open = () => { modal.hidden = false; document.body.classList.add("modal-open"); };
  const close = () => { modal.hidden = true; document.body.classList.remove("modal-open"); };
  openers.forEach((btn) => btn.addEventListener("click", open));
  modal.addEventListener("click", (event) => {
    if (event.target === modal || event.target.closest("[data-help-close]")) close();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !modal.hidden) close();
  });
})();

// Any form carrying data-confirm asks before it submits. Replaces the inline
// onsubmit="return confirm(...)" attributes, which a Content-Security-Policy
// has no way to allow.
(function () {
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
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

  // The sidebar is shown by default on desktop and hidden (off-canvas) on
  // mobile, so "expanded" is derived differently per viewport.
  function syncAria() {
    const shown = isMobile()
      ? shell.classList.contains("sidebar-open")
      : !shell.classList.contains("sidebar-collapsed");
    toggle.setAttribute("aria-expanded", String(shown));
  }

  toggle.addEventListener("click", () => {
    // Mobile slides the sidebar in; desktop collapses it out of the way.
    shell.classList.toggle(isMobile() ? "sidebar-open" : "sidebar-collapsed");
    syncAria();
  });
  overlay.addEventListener("click", () => {
    shell.classList.remove("sidebar-open");
    syncAria();
  });
  window.addEventListener("resize", syncAria);
  syncAria();
})();
