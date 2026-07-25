// The device-link alarm shared by the Manual and Auto timing views.
//
// A timing device that has lost its connection records nothing, and the only
// other place that shows it is the Timing settings page — which nobody has open
// during a run. Both live payloads carry `device_link` (apps/timing/cp540.py
// link_state), and each view calls renderDeviceAlarm() from its own render pass,
// so the banner appears on the next WebSocket nudge after the link drops.
(function () {
  window.renderDeviceAlarm = function (link) {
    const box = document.getElementById("device-alarm");
    if (!box) return;
    // Not monitored = the selected device holds no connection (the simulator).
    const alarm = !!link && link.monitored && !link.ok;
    box.hidden = !alarm;
    if (!alarm) return;
    const text = document.getElementById("device-alarm-text");
    if (text) {
      text.textContent = link.error ? link.message + " (" + link.error + ")" : link.message;
    }
    // Reconnecting reads as "working on it"; anything else is a dead link.
    box.classList.toggle("device-alarm--retrying", link.status === "reconnecting"
      || link.status === "connecting");
  };
})();
