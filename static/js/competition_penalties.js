/* Competition Setup > Penalties: adding and removing marshal posts, the
 * task-spec summary, and the dialog that says what turning posts off destroys.
 *
 * Was inline in its template; see static/js/shell.js for why nothing is.
 */

(function () {
  // A file is loaded on every render of the page, where the block it came from
  // was rendered only alongside the form. Check rather than assume.
  const form = document.getElementById("penalties-form");
  if (!form) return;
  const toggle = document.getElementById("penalties-toggle");
  const config = form.querySelector("[data-posts-config]");
  const postsWrap = form.querySelector("[data-posts]");
  const countInput = document.getElementById("post-count");
  const summaryEl = form.querySelector("[data-tasks-summary]");
  const stopLineField = document.getElementById("stop-line-post");
  const template = document.getElementById("post-template");
  const saveBtn = document.getElementById("penalties-save");
  const MAX = (window.pageData("page-config") || {}).maxPosts;

  // Mirror of taskspec.parse: text -> sorted array, or null if malformed.
  function parseSpec(text) {
    const nums = new Set();
    if (!text) return [];
    for (const token of text.split(",")) {
      const t = token.trim();
      if (!t) continue;
      const m = t.match(/^(\d+)\s*(?:-\s*(\d+))?$/);
      if (!m) return null;
      let lo = parseInt(m[1], 10);
      let hi = m[2] !== undefined ? parseInt(m[2], 10) : lo;
      if (hi < lo) { const s = lo; lo = hi; hi = s; }
      if (lo < 1) return null;
      for (let i = lo; i <= hi; i++) nums.add(i);
    }
    return Array.from(nums).sort((a, b) => a - b);
  }

  function formatRanges(sorted) {
    const spans = [];
    for (const n of sorted) {
      const last = spans[spans.length - 1];
      if (last && n === last[1] + 1) last[1] = n;
      else spans.push([n, n]);
    }
    return spans.map(([a, b]) => (a === b ? String(a) : a + "-" + b)).join(", ");
  }

  function posts() { return Array.from(postsWrap.querySelectorAll("[data-post]")); }

  // Renumber every post block 1..n so the field names, titles and stop-line
  // values stay contiguous no matter how blocks were added or removed.
  function renumber() {
    posts().forEach((post, index) => {
      const n = index + 1;
      post.dataset.number = n;
      post.querySelector("[data-post-title]").textContent = gettext("Marshal Post") + " " + n;
      post.querySelector("[data-tasks]").name = "post-" + n + "-tasks";
      post.querySelector("[data-stop-line]").value = n;
    });
  }

  function updateSummary() {
    const postEls = posts();
    const parsedList = postEls.map((p) => parseSpec(p.querySelector("[data-tasks]").value));
    const malformed = parsedList.some((arr) => arr === null);

    // A task may only be watched by one post: count each number across posts
    // and flag any that appears more than once.
    const counts = new Map();
    parsedList.forEach((arr) => {
      if (arr) arr.forEach((n) => counts.set(n, (counts.get(n) || 0) + 1));
    });
    const dupes = new Set([...counts].filter(([, c]) => c > 1).map(([n]) => n));

    const all = new Set();
    postEls.forEach((post, i) => {
      const input = post.querySelector("[data-tasks]");
      const errEl = post.querySelector("[data-post-error]");
      const arr = parsedList[i];
      if (arr === null) {
        input.classList.add("input--bad");
        errEl.textContent = gettext("This task list can’t be read.");
        return;
      }
      const overlap = arr.filter((n) => dupes.has(n));
      if (overlap.length) {
        input.classList.add("input--bad");
        errEl.textContent = gettext("Task {tasks} is on another post too.").replace("{tasks}", formatRanges(overlap));
      } else {
        input.classList.remove("input--bad");
        errEl.textContent = "";
      }
      arr.forEach((n) => all.add(n));
    });

    if (malformed) {
      summaryEl.textContent = gettext("Check the task lists — one can’t be read.");
      summaryEl.classList.add("tasks-summary--bad");
    } else if (dupes.size) {
      summaryEl.textContent = gettext("Task {tasks} assigned to more than one post.")
        .replace("{tasks}", formatRanges([...dupes].sort((a, b) => a - b)));
      summaryEl.classList.add("tasks-summary--bad");
    } else {
      summaryEl.classList.remove("tasks-summary--bad");
      const sorted = [...all].sort((a, b) => a - b);
      summaryEl.textContent = sorted.length
        ? gettext("Tasks {tasks} assigned").replace("{tasks}", formatRanges(sorted))
        : gettext("No tasks assigned yet");
    }

    // A duplicate or unreadable list must not be saved.
    saveBtn.disabled = malformed || dupes.size > 0;
  }

  // Only one post can own the stop line: checking one clears and disables the
  // rest. Keep the hidden field pointing at the checked post (or empty).
  function syncStopLine(changed) {
    const boxes = posts().map((p) => p.querySelector("[data-stop-line]"));
    if (changed && changed.checked) {
      boxes.forEach((b) => { if (b !== changed) b.checked = false; });
    }
    const chosen = boxes.find((b) => b.checked);
    boxes.forEach((b) => {
      const lock = chosen && b !== chosen;
      b.disabled = lock;
      b.closest(".switch").classList.toggle("switch--disabled", !!lock);
    });
    stopLineField.value = chosen ? chosen.value : "";
  }

  function setCount(n) {
    n = Math.max(1, Math.min(MAX, n | 0));
    let current = posts().length;
    while (current > n) { postsWrap.lastElementChild.remove(); current--; }
    while (current < n) {
      const frag = template.content.cloneNode(true);
      postsWrap.appendChild(frag);
      current++;
    }
    renumber();
    syncStopLine(null);
    updateSummary();
  }

  function applyToggle() {
    const on = toggle.checked;
    config.hidden = !on;
    postsWrap.hidden = !on;
  }

  toggle.addEventListener("change", applyToggle);
  countInput.addEventListener("input", () => {
    if (countInput.value === "") return;   // let the field be cleared mid-edit
    setCount(parseInt(countInput.value, 10));
  });
  countInput.addEventListener("change", () => setCount(parseInt(countInput.value, 10) || 1));
  postsWrap.addEventListener("input", (event) => {
    if (event.target.matches("[data-tasks]")) updateSummary();
  });
  postsWrap.addEventListener("change", (event) => {
    if (event.target.matches("[data-stop-line]")) syncStopLine(event.target);
  });

  // ---- confirm before recorded penalties are destroyed ----
  // Turning the toggle off deletes every post; reducing the count deletes the
  // ones past it. Their MarshalPenalty rows CASCADE away with them, so the
  // number is put in front of the operator first. The view checks the same
  // thing, so a stale page or a JS-less post can't slip past.
  const lossModal = document.getElementById("penalty-loss-modal");
  const lossBody = lossModal && lossModal.querySelector("[data-penalty-loss-body]");
  const lossField = document.getElementById("confirm-penalty-loss");
  const penaltyCounts = JSON.parse(document.getElementById("penalty-counts").textContent);

  // How many recorded penalties the form as it stands would delete.
  function penaltiesAtRisk() {
    const keep = toggle.checked ? posts().length : 0;
    let total = 0;
    Object.keys(penaltyCounts).forEach((number) => {
      if (parseInt(number, 10) > keep) total += penaltyCounts[number];
    });
    return total;
  }

  function openLossModal(count) {
    if (!lossModal) return;
    if (lossBody) {
      lossBody.innerHTML = (count === 1
        ? gettext("This will delete <strong>{n}</strong> penalty already recorded by a marshal post.")
        : gettext("This will delete <strong>{n}</strong> penalties already recorded by marshal posts.")
      ).replace("{n}", count);
    }
    lossModal.hidden = false;
    document.body.classList.add("modal-open");
  }
  function closeLossModal() {
    if (!lossModal) return;
    lossModal.hidden = true;
    document.body.classList.remove("modal-open");
  }

  form.addEventListener("submit", (event) => {
    if (lossField.value) return;             // already confirmed
    const count = penaltiesAtRisk();
    if (!count) return;
    event.preventDefault();
    openLossModal(count);
  });

  if (lossModal) {
    lossModal.querySelector("[data-penalty-loss-confirm]").addEventListener("click", () => {
      lossField.value = "1";
      closeLossModal();
      form.submit();
    });
    lossModal.querySelector("[data-penalty-loss-cancel]").addEventListener("click", closeLossModal);
    lossModal.addEventListener("click", (event) => {
      if (event.target === lossModal) closeLossModal();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !lossModal.hidden) closeLossModal();
    });
    // Rendered already open: the view refused a destructive save.
    if (!lossModal.hidden) document.body.classList.add("modal-open");
  }

  applyToggle();
  syncStopLine(null);
  updateSummary();
})();
