/* Competition Setup > General. Only the type-change dialog: the server is what
refuses an unconfirmed change, this re-submits once the answer is given.
 */

// The server is what refuses an unconfirmed change (a stale page must not be
// able to skip it); this only re-submits with the answer once it is given.
(function () {
  const modal = document.getElementById("type-change-modal");
  const form = document.getElementById("general-form");
  const flag = document.getElementById("confirm-type-change");
  if (!modal || !form || !flag) return;
  // Rendered already open by the server when the answer is missing, which is
  // why the controller focuses a dialog it finds open — see shell.js.
  const dialog = window.modalController(modal, { onClose: () => { flag.value = ""; } });
  modal.querySelector("[data-type-change-confirm]").addEventListener("click", () => {
    flag.value = "1";
    form.requestSubmit();
  });
  modal.querySelector("[data-type-change-cancel]").addEventListener("click", dialog.close);
})();
