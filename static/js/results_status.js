// "DNS" buttons in the results table's not-yet-ranked block.
//
// A competitor sits there because a counted run is neither timed nor marked. The
// button says the run was never started: it posts the run's *start-order slot*
// (not a run id — nothing was ever recorded for it, so there is no row yet) to
// timing:run-status, which makes the run and closes it DNS. The page reloads,
// because one mark can settle the competitor's whole event and move their row
// into the final result.
(function () {
  const config = document.getElementById("run-status");
  const editor = document.getElementById("tie-editor");
  if (!config) return;

  const url = config.dataset.url;
  const confirmText = config.dataset.confirm;
  // The one CSRF token on the page — the tie editor renders it.
  const csrf = editor && editor.querySelector("[name=csrfmiddlewaretoken]");

  document.addEventListener("click", async (event) => {
    const button = event.target.closest(".rt-dns");
    if (!button || button.disabled) return;
    if (confirmText) {
      const agreed = await window.appConfirm({
        body: confirmText,
        accept: gettext("Mark as DNS"),
      });
      if (!agreed) return;
    }
    button.disabled = true;
    let res;
    try {
      res = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrf ? csrf.value : "",
        },
        body: JSON.stringify({ slot_key: button.dataset.dnsKey, status: "dns" }),
      }).then((r) => r.json());
    } catch (err) {
      res = { ok: false };
    }
    if (res && res.ok) {
      window.location.reload();
    } else {
      // Left in place with the button usable again — nothing was recorded.
      button.disabled = false;
      button.classList.add("rt-dns--failed");
    }
  });
})();
