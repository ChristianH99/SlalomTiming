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
  modal.querySelector("[data-type-change-confirm]").addEventListener("click", () => {
    flag.value = "1";
    form.requestSubmit();
  });
  const cancel = () => { modal.hidden = true; flag.value = ""; };
  modal.querySelector("[data-type-change-cancel]").addEventListener("click", cancel);
  modal.addEventListener("click", (e) => { if (e.target === modal) cancel(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.hidden) cancel();
  });
})();
