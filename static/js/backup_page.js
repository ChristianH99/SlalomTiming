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
      const editing = document.activeElement;
      const typing = editing && ["INPUT", "TEXTAREA"].includes(editing.tagName);
      if (!typing) window.location.reload();
    }
  }

  const el = document.querySelector("[data-backup-alert]");
  if (el) setInterval(poll, 10000);
})();
