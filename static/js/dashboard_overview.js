// Organiser dashboard: a live, read-only overview of the active competition —
// headline stats, overall run progress, per-class status, and the competitor on
// course. State is served as JSON (json_script) and re-fetched on every WebSocket
// nudge (the shared timing_live group), so the view keeps up as times land.
(function () {
  "use strict";

  const URLS = window.DASH_URLS;
  const dataEl = document.getElementById("dash-data");
  if (!URLS || !dataEl) return;

  let state = JSON.parse(dataEl.textContent);

  const statsEl = document.getElementById("dash-stats");
  const progressEl = document.getElementById("dash-progress");
  const currentEl = document.getElementById("dash-current");
  const classesEl = document.getElementById("dash-classes");
  const dot = document.getElementById("live-dot");
  const updatedEl = document.getElementById("dash-updated");

  // Status → icon + human label. Colour is carried by a class on the element, but
  // the icon and label mean the state is never conveyed by colour alone.
  const STATUS = {
    done: { icon: "✓", label: gettext("Done") },
    running: { icon: "●", label: gettext("Running") },
    not_started: { icon: "○", label: gettext("To come") },
  };

  // ---- small DOM helpers --------------------------------------------------
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  // ---- stat tiles ---------------------------------------------------------
  function renderStats() {
    const s = state.stats;
    const tiles = [
      { label: gettext("Participants"), value: s.participants,
        sub: s.did_not_run
          ? interpolate(gettext("%(n)s did not run"), { n: s.did_not_run }, true)
          : gettext("all racing") },
      { label: gettext("Runs completed"), value: `${s.runs_finished}`,
        sub: interpolate(gettext("of %(total)s · %(remaining)s to go"),
          { total: s.runs_expected, remaining: s.runs_remaining }, true) },
      { label: gettext("Classes finished"), value: `${s.classes_done}`,
        sub: interpolate(gettext("of %(total)s"), { total: s.classes_total }, true) },
    ];
    if (s.marshal_posts) {
      tiles.push({ label: gettext("Marshal posts"), value: s.marshal_posts, sub: gettext("reporting") });
    }
    statsEl.replaceChildren(...tiles.map(renderTile));
  }

  function renderTile(t) {
    const tile = el("div", "stat-tile");
    tile.append(
      el("span", "stat-tile-label", t.label),
      el("span", "stat-tile-value", String(t.value)),
      el("span", "stat-tile-sub", t.sub),
    );
    return tile;
  }

  // ---- overall progress ring ----------------------------------------------
  function renderProgress() {
    const p = state.progress;
    const r = 54;
    const c = 2 * Math.PI * r;
    const offset = c * (1 - p.percent / 100);

    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", "0 0 128 128");
    svg.setAttribute("class", "ring");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label",
      interpolate(gettext("%(percent)s% of runs completed"), { percent: p.percent }, true));

    const track = document.createElementNS(svgNS, "circle");
    const arc = document.createElementNS(svgNS, "circle");
    for (const circle of [track, arc]) {
      circle.setAttribute("cx", "64");
      circle.setAttribute("cy", "64");
      circle.setAttribute("r", String(r));
    }
    track.setAttribute("class", "ring-track");
    arc.setAttribute("class", "ring-arc");
    arc.setAttribute("stroke-dasharray", String(c));
    arc.setAttribute("stroke-dashoffset", String(offset));
    if (p.percent >= 100 && p.expected > 0) arc.classList.add("ring-arc--done");
    svg.append(track, arc);

    const center = el("div", "ring-center");
    center.append(
      el("span", "ring-percent", `${p.percent}%`),
      el("span", "ring-caption", gettext("of runs done")),
    );

    const wrap = el("div", "ring-wrap");
    wrap.append(svg, center);

    const legend = el("div", "dash-progress-legend");
    legend.append(
      el("span", "dash-progress-count", `${p.finished} / ${p.expected}`),
      el("span", "dash-progress-caption", gettext("runs completed")),
    );

    progressEl.replaceChildren(wrap, legend);
  }

  // ---- current competitor -------------------------------------------------
  function renderCurrent() {
    const c = state.current;
    if (!c) {
      currentEl.replaceChildren(
        el("p", "dash-empty", gettext("No one is on course yet."))
      );
      return;
    }

    const head = el("div", "dash-current-head");
    if (c.bib != null) head.append(el("span", "dash-current-bib", `#${c.bib}`));
    const who = el("div", "dash-current-who");
    who.append(el("span", "dash-current-name", c.name || "—"));
    const meta = [c.class_name, c.run_label].filter(Boolean).join(" · ");
    if (meta) who.append(el("span", "dash-current-meta", meta));
    if (c.club) who.append(el("span", "dash-current-club", c.club));
    head.append(who);

    // A state badge that mirrors the class status vocabulary.
    let badgeKey = "not_started", badgeText = gettext("Ready to start");
    if (c.finished) { badgeKey = "done"; badgeText = gettext("Finished"); }
    else if (c.started) { badgeKey = "running"; badgeText = gettext("On course"); }
    const badge = el("span", `dash-badge dash-badge--${badgeKey}`);
    badge.append(el("span", "dash-badge-dot", STATUS[badgeKey].icon), document.createTextNode(badgeText));
    head.append(badge);

    const body = el("div", "dash-current-body");
    if (c.finished) {
      body.append(figure(gettext("Run time"), c.run_time || "—"));
      body.append(figure(gettext("Total time"), c.total_time || "—", "primary"));
      if (state.penalties_enabled) {
        // Already rendered by calc.format_penalty — the notation lives there.
        body.append(figure(gettext("Penalties"), c.penalty || "—"));
      }
    } else {
      const note = el("p", "dash-current-note",
        c.started ? gettext("Timing in progress — result shown when the run finishes.")
                  : gettext("Waiting for the start signal."));
      body.append(note);
    }
    currentEl.replaceChildren(head, body);

    if (c.finished && state.penalties_enabled &&
        (c.pylons || c.tasks || c.stop)) {
      const chips = el("div", "dash-pen-chips");
      if (c.pylons) chips.append(el("span", "dash-pen-chip",
        interpolate(gettext("%(n)s × pylon"), { n: c.pylons }, true)));
      if (c.tasks) chips.append(el("span", "dash-pen-chip",
        interpolate(gettext("%(n)s × task"), { n: c.tasks }, true)));
      if (c.stop) chips.append(el("span", "dash-pen-chip", gettext("stop line")));
      currentEl.append(chips);
    }
  }

  function figure(label, value, tone) {
    const fig = el("div", "dash-figure" + (tone ? ` dash-figure--${tone}` : ""));
    fig.append(el("span", "dash-figure-label", label),
               el("span", "dash-figure-value", value));
    return fig;
  }

  // ---- classes board ------------------------------------------------------
  function renderClasses() {
    if (!state.classes.length) {
      classesEl.replaceChildren(el("p", "dash-empty", gettext("No running classes yet.")));
      return;
    }
    classesEl.replaceChildren(...state.classes.map(renderClassRow));
  }

  function renderClassRow(cls) {
    const meta = STATUS[cls.status] || STATUS.not_started;
    const row = el("div", `dash-class dash-class--${cls.status}`);

    const status = el("span", "dash-class-status");
    status.append(el("span", "dash-class-icon", meta.icon),
                  el("span", "dash-class-state", meta.label));

    const main = el("div", "dash-class-main");
    const top = el("div", "dash-class-top");
    top.append(el("span", "dash-class-name", cls.name));
    top.append(el("span", "dash-class-count",
      cls.total
        ? interpolate(gettext("%(finished)s / %(total)s runs"),
            { finished: cls.finished, total: cls.total }, true)
        : gettext("no starters")));
    main.append(top);

    const bar = el("div", "dash-bar");
    const fill = el("span", "dash-bar-fill");
    fill.style.width = `${cls.percent}%`;
    bar.append(fill);
    main.append(bar);

    const foot = el("div", "dash-class-foot");
    foot.append(el("span", "dash-class-scoring", cls.scoring));
    if (cls.status === "running") {
      foot.append(el("span", "dash-class-percent", `${cls.percent}%`));
    }
    main.append(foot);

    row.append(status, main);
    return row;
  }

  // ---- render everything --------------------------------------------------
  function render() {
    renderStats();
    renderProgress();
    renderCurrent();
    renderClasses();
    if (updatedEl) {
      updatedEl.textContent = interpolate(gettext("Updated %(time)s"),
        { time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) }, true);
    }
  }

  // ---- live refresh (WebSocket nudge) -------------------------------------
  async function refresh() {
    try {
      const res = await fetch(URLS.state, { headers: { "X-Requested-With": "fetch" } });
      const data = await res.json();
      if (data.competition === false) { window.location.reload(); return; }
      state = data;
      render();
    } catch (e) {
      /* transient fetch error — the next nudge will retry */
    }
  }

  render();
  // Socket lifecycle and the re-fetch after an outage: see live_socket.js. This
  // page shows its state in the heading dot rather than the shared banner, so it
  // drives the dot from the module's state callback.
  window.liveSocket({
    onRefresh: refresh,
    onState: (state) => {
      if (dot) dot.classList.toggle("connected", state === "online");
    },
  });
})();
