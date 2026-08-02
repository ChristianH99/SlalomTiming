/* The read-only "settings this event was run under" pop-up.
 *
 * Rendered already open by the server, so all this does is hand it to
 * modalController — which is what gives a server-opened dialog its focus
 * management, its Tab wrapping and its Escape. Nothing here is editable, so
 * there is nothing else to wire.
 */
(function () {
  const modal = document.querySelector("[data-modal-open]");
  if (modal) window.modalController(modal);
})();
