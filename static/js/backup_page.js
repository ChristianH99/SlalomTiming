/* Backup → Automatic backup: keep the "is it working" panel current.
 *
 * The page's whole job is to answer one question — is a copy being written? — and
 * the answer changes without anybody touching the page: the stick gets pulled out,
 * the disk fills, the next copy lands. So it polls rather than waiting for a
 * reload. Slowly: the interval is minutes, not seconds.
 */

(function () {
  const URLS = window.pageData("page-urls");
  const alert = document.querySelector("[data-backup-alert]");
  if (!URLS || !URLS.status) return;

  let lastRun = alert ? alert.dataset.lastRun : null;

  async function poll() {
    let data;
    try {
      data = await fetch(URLS.status, { headers: { Accept: "application/json" } })
        .then((r) => r.json());
    } catch (err) {
      return;   // transient; the next tick will do
    }
    // Only reload when something actually happened, so a page somebody is typing
    // a destination into is not yanked out from under them every few seconds.
    if (data.last_run_at && data.last_run_at !== lastRun) {
      lastRun = data.last_run_at;
      if (!busy()) window.location.reload();
    }
  }

  /* Anything the operator is in the middle of. A copy every minute means a reload
   * every minute, and at first this only checked for typing — so the folder picker
   * closed under whoever was three folders deep in it. Any open dialog counts. */
  function busy() {
    const focused = document.activeElement;
    if (focused && ["INPUT", "TEXTAREA", "SELECT"].includes(focused.tagName)) return true;
    return Boolean(document.querySelector(".modal-overlay:not([hidden])"));
  }

  const el = document.querySelector("[data-backup-alert]");
  if (el) setInterval(poll, 10000);
})();

/* The destination picker.
 *
 * Browsing the *server's* folders, not the browser's. A file input would offer
 * whichever machine is displaying the page, and over the venue network that is
 * usually somebody else's phone — a path from there means nothing to the laptop
 * doing the writing. So every list comes from transfer:backup-folders.
 */

(function () {
  const URLS = window.pageData("page-urls");
  const modal = document.querySelector("[data-folder-modal]");
  const field = document.querySelector("[name='destination']");
  if (!URLS || !URLS.folders || !modal || !field) return;

  const list = modal.querySelector("[data-folder-list]");
  const here = modal.querySelector("[data-folder-here]");
  const up = modal.querySelector("[data-folder-up]");
  const problem = modal.querySelector("[data-folder-problem]");
  const truncated = modal.querySelector("[data-folder-truncated]");
  const use = modal.querySelector("[data-folder-use]");

  let current = "";
  let parent = "";

  function render(data) {
    current = data.path || "";
    parent = data.parent || "";
    here.textContent = current || data.label;
    // A path is read exactly as written and gets the monospace treatment; the
    // drive list's "This computer" is a label, and looked like a typo in it.
    here.classList.toggle("folder-here--path", Boolean(current));
    up.hidden = !parent && !current;      // from a drive, "Up" goes to the drive list
    // Nothing to choose at the drive list: it is not a folder anybody backs up to.
    use.disabled = !current;

    problem.hidden = !data.problem;
    problem.textContent = data.problem || "";

    list.replaceChildren();
    if (!data.entries.length && !data.problem) {
      const empty = document.createElement("li");
      empty.className = "folder-empty";
      empty.textContent = gettext("No folders in here.");
      list.appendChild(empty);
    }
    data.entries.forEach((entry) => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "folder-entry";
      button.textContent = entry.name;
      button.addEventListener("click", () => open(entry.path));
      item.appendChild(button);
      list.appendChild(item);
    });

    truncated.hidden = !data.truncated;
    if (data.truncated) {
      truncated.textContent = interpolate(
        ngettext("%s more folder is not shown.", "%s more folders are not shown.",
                 data.truncated),
        [data.truncated]);
    }
  }

  async function open(path) {
    const url = URLS.folders + "?path=" + encodeURIComponent(path || "");
    try {
      render(await fetch(url, { headers: { Accept: "application/json" } })
        .then((r) => r.json()));
    } catch (err) {
      problem.hidden = false;
      problem.textContent = gettext("Could not read the folders on the server.");
    }
  }

  // Focus in, trapped, and handed back to the Browse button on close — see
  // shell.js::modalController, which every dialog in the app goes through.
  const dialog = window.modalController(modal);

  function show() {
    dialog.open();
    // Start where the field already points, so re-picking is one step from where
    // they were rather than back at the drive list.
    open(field.value.trim());
  }

  document.querySelectorAll("[data-folder-open]").forEach((button) =>
    button.addEventListener("click", show));
  modal.querySelectorAll("[data-folder-close]").forEach((button) =>
    button.addEventListener("click", dialog.close));
  up.addEventListener("click", () => open(parent));
  use.addEventListener("click", () => {
    field.value = current;
    // The unsaved-changes guard watches for input events, not assignments.
    field.dispatchEvent(new Event("input", { bubbles: true }));
    dialog.close();
  });
})();
