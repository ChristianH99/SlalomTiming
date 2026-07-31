/* A competition type's settings page: greying the penalty amounts out as the
 * penalties toggle moves.
 *
 * Was inline in the template, where it could look the checkbox up by the id
 * Django rendered for it. A file cannot be handed a template variable, so the
 * markup carries a `data-penalty-toggle` marker instead — which is the better
 * coupling anyway: the script no longer depends on how the form names its
 * fields.
 */

(function () {
  // The amounts only mean something with penalties on — grey them out with
  // the toggle rather than hiding them, so the numbers stay visible. They
  // are disabled too, so nothing can be typed into a value that the form
  // would then discard.
  const toggle = document.querySelector("[data-penalty-toggle] input");
  const block = document.querySelector("[data-penalty-block]");
  if (!toggle || !block) return;
  const apply = () => {
    const off = !toggle.checked;
    block.classList.toggle("settings-row--off", off);
    block.querySelectorAll("input").forEach((input) => { input.disabled = off; });
  };
  toggle.addEventListener("change", apply);
  apply();
})();
