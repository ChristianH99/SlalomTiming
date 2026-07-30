/* The add/edit participant form's page-level behaviour: the live class preview
 * that follows the date of birth, and the duplicate check.
 *
 * Was inline in its template. Nothing on a page may be inline now that the app
 * ships a Content-Security-Policy, so the page's numbers arrive through
 * `json_script` and its strings through `gettext()` (the djangojs catalog that
 * base.html loads).
 */

(function () {
  // The dialog is only rendered when the server is asking about a bib change, so
  // on an ordinary add or edit it is not there. This block used to sit inside the
  // template's own {% if %} and could assume it existed; a file is loaded on every
  // render of the page and has to check.
  const modal = document.getElementById("bib-change-modal");
  const cancel = modal && modal.querySelector("[data-bib-change-cancel]");
  if (!cancel) return;
  // Rendered already open — the server is asking — so the controller focuses it
  // where it stands (shell.js). Cancel leaves the form exactly as it was posted,
  // so the bib can be put back (or anything else corrected) before trying again.
  const dialog = window.modalController(modal);
  cancel.addEventListener("click", dialog.close);
})();

(function () {
  const dobInput = document.getElementById("id_date_of_birth");
  if (!dobInput) return;

  const dobError = document.getElementById("dob-error");
  const classOutput = document.getElementById("class-preview-value");
  const ranges = JSON.parse(document.getElementById("class-ranges-data").textContent);
  const year = (window.pageData("page-config") || {}).competitionYear;

  // A native date input can report badInput=false yet still leave a
  // malformed value (e.g. typing a stray extra digit into the month
  // segment can spill into the year, producing "92022-09-15"). Only
  // trust a value that's exactly YYYY-MM-DD with a 4-digit year.
  function isMalformed() {
    return dobInput.value !== "" && !/^\d{4}-\d{2}-\d{2}$/.test(dobInput.value);
  }

  // Chrome's year segment doesn't cap itself at 4 digits once it's
  // fully selected before typing — clamp back to the first 4 digits on blur.
  function clampYearOverflow() {
    const match = dobInput.value.match(/^(\d{4})(\d+)-(\d{2})-(\d{2})$/);
    if (match) {
      dobInput.value = `${match[1]}-${match[3]}-${match[4]}`;
    }
  }

  function checkValidity() {
    const malformed = isMalformed();
    dobInput.setCustomValidity(malformed ? gettext("Invalid date") : "");
    if (!dobError) return;
    dobError.textContent = dobInput.validity.badInput || malformed
      ? gettext("That’s not a valid date — please check the day exists in that month/year.")
      : "";
  }

  function updateClass() {
    if (!classOutput) return;
    const val = dobInput.value;
    if (!val || !year || isMalformed()) { classOutput.textContent = "–"; return; }
    const birthYear = parseInt(val.split("-")[0], 10);
    if (Number.isNaN(birthYear)) { classOutput.textContent = "–"; return; }
    const age = year - birthYear;
    const match = ranges.find((r) => age >= r.age_from && age <= r.age_to);
    classOutput.textContent = match ? match.label : "–";
  }

  dobInput.addEventListener("input", () => { checkValidity(); updateClass(); });
  dobInput.addEventListener("blur", () => { clampYearOverflow(); checkValidity(); updateClass(); });
  checkValidity();
  updateClass();
})();

// Manual class picker: add classes from the dropdown as removable chips.
// Distinct-class and per-class repeat limits mirror the server-side rules.
(function () {
  const picker = document.querySelector("[data-class-picker]");
  if (!picker) return;
  const chips = picker.querySelector("[data-picker-chips]");
  const select = picker.querySelector("[data-picker-add]");
  const allowMultiple = picker.dataset.allowMultiple === "1";

  const chipEls = () => [...chips.querySelectorAll("[data-chip]")];
  const distinctCount = () => new Set(chipEls().map((c) => c.dataset.pk)).size;
  const count = (pk) => chipEls().filter((c) => c.dataset.pk === pk).length;
  function repeatable(pk) {
    const o = select.querySelector(`option[value="${pk}"]`);
    return !!o && o.dataset.repeat === "1";
  }
  function canAdd(pk) {
    const present = count(pk) > 0;
    if (present && !repeatable(pk)) return false;
    if (!present && distinctCount() >= 1 && !allowMultiple) return false;
    return true;
  }
  function refreshOptions() {
    select.querySelectorAll("option[value]").forEach((o) => {
      if (o.value) o.disabled = !canAdd(o.value);
    });
  }
  function addChip(pk, name) {
    const chip = document.createElement("span");
    chip.className = "class-chip";
    chip.dataset.chip = "";
    chip.dataset.pk = pk;
    chip.append(name + " ");
    const input = document.createElement("input");
    input.type = "hidden"; input.name = "classes"; input.value = pk;
    const btn = document.createElement("button");
    btn.type = "button"; btn.className = "chip-remove"; btn.dataset.chipRemove = "";
    btn.setAttribute("aria-label", gettext("Remove")); btn.innerHTML = "&times;";
    chip.append(input, btn);
    chips.appendChild(chip);
  }
  select.addEventListener("change", () => {
    const pk = select.value;
    if (pk && canAdd(pk)) addChip(pk, select.options[select.selectedIndex].textContent.trim());
    select.value = "";
    refreshOptions();
  });
  chips.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-chip-remove]");
    if (!btn) return;
    btn.closest("[data-chip]").remove();
    refreshOptions();
    // Removing a chip fires no input/change event of its own.
    document.dispatchEvent(new Event("unsaved-change"));
  });
  refreshOptions();
})();
