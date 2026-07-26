// The single-light-barrier phase indicator, shared by the Manual and Auto timing
// views (templates/timing/_barrier_phase.html).
//
// With start_channel == finish_channel one channel does both jobs and the role
// alternates in software (apps/timing/arrangement.py effective_role). That makes
// the phase live state with nothing showing it: a spurious pulse opens a run
// nobody ran, and from then on every start is recorded as a finish for the rest
// of the event. Both live payloads carry `barrier` (autotiming.barrier_phase),
// each view calls renderBarrierPhase() from its own render pass, and a two-channel
// rig sends null — where a signal's role comes from its port, there is no phase.
(function () {
  window.renderBarrierPhase = function (barrier) {
    const box = document.getElementById("barrier-phase");
    if (!box) return;
    box.hidden = !barrier;
    if (!barrier) return;
    const role = document.getElementById("barrier-phase-role");
    const finish = barrier.next_role === "finish";
    if (role) role.textContent = finish ? gettext("Finish") : gettext("Start");
    box.classList.toggle("barrier-phase--finish", finish);
  };
})();
