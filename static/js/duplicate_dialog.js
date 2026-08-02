/* The "duplicate an archived event" dialog.
 *
 * Rendered already open by the server (competition_duplicate.html), like the
 * type-change and penalty-loss confirmations — modalController is what focuses
 * a dialog it finds open and wraps Tab inside it. The backdrop does not close
 * it (a stray click on a question with radio buttons in it should not answer
 * the question), but Escape still does, and the page behind says how to get the
 * dialog back rather than being blank.
 *
 * The only behaviour of its own: the settings reconciliation applies to the
 * full copy and not to the setup-only one. A copy made to set up next season
 * should run under this season's rules, so asking which of the archived values
 * to keep would be a question with no right answer.
 */
(function () {
  const modal = document.querySelector("[data-modal-open]");
  if (!modal) return;
  window.modalController(modal, { backdropCloses: false });

  const settings = modal.querySelector("[data-dup-settings]");
  if (!settings) return;

  const modes = modal.querySelectorAll("[data-dup-mode]");

  function sync() {
    const full = Array.from(modes).some((m) => m.checked && m.value === "full");
    settings.hidden = !full;
    // A hidden radio still posts. Disabling the whole block keeps the server's
    // view of what was chosen the same as the operator's — otherwise switching
    // to "setup only" would silently carry the settings answers with it.
    settings.querySelectorAll("input").forEach((input) => {
      input.disabled = !full;
    });
  }

  modes.forEach((mode) => mode.addEventListener("change", sync));
  sync();
})();
