/* Manage competitions: the client-side search filter over the tiles.
 */

(function () {
  // The form filters in place and must never navigate. Was an inline
  // onsubmit="return false", which a Content-Security-Policy blocks.
  const form = document.getElementById("competition-search");
  if (form) form.addEventListener("submit", (event) => event.preventDefault());

  const input = document.getElementById("competition-search-input");
  const body = document.getElementById("competitions-body");
  if (!input || !body) return;
  const noResults = body.querySelector(".no-results-row");
  const tiles = Array.from(body.querySelectorAll("[data-search]"));

  function apply() {
    const term = input.value.trim().toLowerCase();
    let visible = 0;
    for (const tile of tiles) {
      const match = term === "" || tile.dataset.search.includes(term);
      tile.hidden = !match;
      if (match) visible += 1;
    }
    if (noResults) noResults.hidden = !(tiles.length > 0 && visible === 0);
  }

  input.addEventListener("input", apply);
  apply();
})();
