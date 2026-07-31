/* The login form's own behaviour. A file rather than an inline block because the
app ships a Content-Security-Policy (see config/settings.py).
 */

// Press-and-hold the eye icon to reveal the password; release to hide.
(function () {
  const btn = document.querySelector("[data-pw-reveal]");
  if (!btn) return;
  const input = btn.closest(".pw-wrap").querySelector("input");
  const show = () => { input.type = "text"; btn.classList.add("pw-reveal--on"); };
  const hide = () => { input.type = "password"; btn.classList.remove("pw-reveal--on"); };
  btn.addEventListener("pointerdown", (e) => { e.preventDefault(); show(); });
  ["pointerup", "pointerleave", "pointercancel"].forEach((ev) => btn.addEventListener(ev, hide));
  // Keyboard: reveal while Space/Enter is held.
  btn.addEventListener("keydown", (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); show(); } });
  btn.addEventListener("keyup", (e) => { if (e.key === " " || e.key === "Enter") hide(); });
  btn.addEventListener("blur", hide);
})();
