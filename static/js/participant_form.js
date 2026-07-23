(function () {
  "use strict";

  function readJSON(id, fallback) {
    const el = document.getElementById(id);
    if (!el) return fallback;
    try {
      return JSON.parse(el.textContent);
    } catch (e) {
      return fallback;
    }
  }

  const clubs = readJSON("club-options-data", []);
  const emailDomains = readJSON("email-domains-data", []);

  // ---- Club autocomplete (the "memory base") -------------------------------
  // A dropdown of known clubs whose names contain what's been typed. Arrow keys
  // move the highlight, Enter picks the highlighted club. Typing something new
  // and not picking anything keeps the free text, so a new club is added simply
  // by saving the participant.
  function setupClubCombobox() {
    const wrapper = document.querySelector('[data-combobox="club"]');
    if (!wrapper) return;
    const input = wrapper.querySelector("input");
    const list = wrapper.querySelector("[data-combobox-list]");
    if (!input || !list) return;

    let activeIndex = -1;
    let items = [];

    function close() {
      list.hidden = true;
      list.innerHTML = "";
      activeIndex = -1;
      items = [];
      input.setAttribute("aria-expanded", "false");
    }

    function render(matches) {
      list.innerHTML = "";
      matches.forEach((name, i) => {
        const li = document.createElement("li");
        li.className = "combobox-option";
        li.setAttribute("role", "option");
        li.textContent = name;
        li.addEventListener("mousedown", (e) => {
          // mousedown (not click) so it fires before the input's blur.
          e.preventDefault();
          input.value = name;
          close();
          input.dispatchEvent(new Event("input", { bubbles: true }));
        });
        list.appendChild(li);
      });
      items = Array.from(list.children);
      list.hidden = items.length === 0;
      input.setAttribute("aria-expanded", String(!list.hidden));
    }

    function highlight(index) {
      items.forEach((li) => li.classList.remove("active"));
      if (index >= 0 && index < items.length) {
        items[index].classList.add("active");
        items[index].scrollIntoView({ block: "nearest" });
      }
      activeIndex = index;
    }

    function update() {
      const term = input.value.trim().toLowerCase();
      if (term === "") {
        close();
        return;
      }
      const matches = clubs
        .filter((name) => name.toLowerCase().includes(term) && name.toLowerCase() !== term)
        .slice(0, 8);
      render(matches);
      highlight(-1);
    }

    input.addEventListener("input", update);
    input.addEventListener("focus", update);
    input.addEventListener("blur", () => setTimeout(close, 120));
    input.addEventListener("keydown", (e) => {
      if (list.hidden) return;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        highlight((activeIndex + 1) % items.length);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        highlight((activeIndex - 1 + items.length) % items.length);
      } else if (e.key === "Enter") {
        if (activeIndex >= 0) {
          e.preventDefault();
          input.value = items[activeIndex].textContent;
          close();
          input.dispatchEvent(new Event("input", { bubbles: true }));
        } else {
          // Nothing highlighted: keep the typed value, just close the list
          // (don't submit the form yet).
          e.preventDefault();
          close();
        }
      } else if (e.key === "Escape") {
        close();
      }
    });
  }

  // ---- Email-domain completion ---------------------------------------------
  // Once "@" is typed, suggest common domains that match what follows it and
  // let Tab (or Enter / click) complete the address.
  function setupEmailCombobox() {
    const wrapper = document.querySelector('[data-combobox="email"]');
    if (!wrapper) return;
    const input = wrapper.querySelector("input");
    const list = wrapper.querySelector("[data-combobox-list]");
    const hint = wrapper.querySelector("[data-email-hint]");
    if (!input || !list) return;

    let activeIndex = 0;
    let items = [];
    let suggestions = [];

    function close() {
      list.hidden = true;
      list.innerHTML = "";
      items = [];
      suggestions = [];
      activeIndex = 0;
      if (hint) hint.hidden = true;
      input.setAttribute("aria-expanded", "false");
    }

    function localPart() {
      const at = input.value.indexOf("@");
      return at === -1 ? null : input.value.slice(0, at);
    }

    function domainPart() {
      const at = input.value.indexOf("@");
      return at === -1 ? null : input.value.slice(at + 1);
    }

    function accept(domain) {
      input.value = localPart() + "@" + domain;
      close();
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }

    function highlight(index) {
      items.forEach((li) => li.classList.remove("active"));
      if (index >= 0 && index < items.length) items[index].classList.add("active");
      activeIndex = index;
      if (hint && suggestions[index]) {
        hint.hidden = false;
        hint.textContent = interpolate(gettext("Press Tab to complete %(email)s"),
          { email: localPart() + "@" + suggestions[index] }, true);
      }
    }

    function update() {
      const local = localPart();
      const domain = domainPart();
      if (local === null || local === "" || domain === null || domain.includes("@")) {
        close();
        return;
      }
      suggestions = emailDomains
        .filter((d) => d.startsWith(domain.toLowerCase()) && d !== domain.toLowerCase())
        .slice(0, 6);
      list.innerHTML = "";
      suggestions.forEach((domainOption) => {
        const li = document.createElement("li");
        li.className = "combobox-option";
        li.setAttribute("role", "option");
        li.textContent = local + "@" + domainOption;
        li.addEventListener("mousedown", (e) => {
          e.preventDefault();
          accept(domainOption);
        });
        list.appendChild(li);
      });
      items = Array.from(list.children);
      list.hidden = items.length === 0;
      input.setAttribute("aria-expanded", String(!list.hidden));
      if (items.length) highlight(0);
      else if (hint) hint.hidden = true;
    }

    input.addEventListener("input", update);
    input.addEventListener("blur", () => setTimeout(close, 120));
    input.addEventListener("keydown", (e) => {
      if (list.hidden || items.length === 0) return;
      if (e.key === "Tab" || e.key === "Enter") {
        // Tab completes the highlighted suggestion instead of leaving the field.
        e.preventDefault();
        accept(suggestions[activeIndex]);
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        highlight((activeIndex + 1) % items.length);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        highlight((activeIndex - 1 + items.length) % items.length);
      } else if (e.key === "Escape") {
        close();
      }
    });
  }

  // ---- Duplicate detection --------------------------------------------------
  // As soon as first+last name or a licence number are filled in, ask the
  // server whether a matching participant already exists and, if so, show a
  // warning with a link to jump straight to them.
  function setupDuplicateCheck() {
    const form = document.getElementById("participant-form");
    const box = document.getElementById("duplicate-warning");
    if (!form || !box) return;

    const checkUrl = form.dataset.checkUrl;
    const excludePk = form.dataset.excludePk || "";
    const firstName = document.getElementById("id_first_name");
    const lastName = document.getElementById("id_last_name");
    const license = document.getElementById("id_license_number");
    if (!checkUrl || !firstName || !lastName || !license) return;

    let timer = null;

    function render(matches) {
      if (!matches.length) {
        box.hidden = true;
        box.innerHTML = "";
        return;
      }
      const rows = matches
        .map(
          (m) =>
            "<li>" +
            interpolate(
              gettext("Matches <strong>%(name)s</strong>%(club)s on %(reason)s — licence %(license)s. <a href=\"%(url)s\">Jump to this participant</a>"),
              {
                name: escapeHtml(m.name),
                club: m.club ? ` (${escapeHtml(m.club)})` : "",
                reason: escapeHtml(m.reason),
                license: escapeHtml(m.license_number),
                url: m.edit_url,
              }, true) +
            "</li>"
        )
        .join("");
      box.innerHTML =
        gettext("<strong>Possible duplicate.</strong> This looks like someone already registered. You can continue anyway, cancel, or open the existing record:") +
        `<ul class="notice-list">${rows}</ul>`;
      box.hidden = false;
    }

    function run() {
      const params = new URLSearchParams();
      params.set("first_name", firstName.value.trim());
      params.set("last_name", lastName.value.trim());
      params.set("license_number", license.value.trim());
      if (excludePk) params.set("exclude", excludePk);

      const hasName = firstName.value.trim() && lastName.value.trim();
      const hasLicense = license.value.trim();
      if (!hasName && !hasLicense) {
        render([]);
        return;
      }

      fetch(checkUrl + "?" + params.toString(), { headers: { "X-Requested-With": "fetch" } })
        .then((r) => (r.ok ? r.json() : { matches: [] }))
        .then((data) => render(data.matches || []))
        .catch(() => render([]));
    }

    function schedule() {
      clearTimeout(timer);
      timer = setTimeout(run, 300);
    }

    [firstName, lastName, license].forEach((el) => el.addEventListener("input", schedule));
  }

  function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  setupClubCombobox();
  setupEmailCombobox();
  setupDuplicateCheck();
})();
