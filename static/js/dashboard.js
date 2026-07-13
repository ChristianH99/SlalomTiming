(function () {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}/ws/timing/`);
  const dot = document.getElementById("live-dot");
  const body = document.getElementById("events-body");

  socket.addEventListener("open", () => dot.classList.add("connected"));
  socket.addEventListener("close", () => dot.classList.remove("connected"));

  socket.addEventListener("message", (message) => {
    const event = JSON.parse(message.data);
    const emptyRow = body.querySelector("td[colspan]");
    if (emptyRow) emptyRow.closest("tr").remove();

    const row = document.createElement("tr");
    row.id = "new-row";
    const receivedAt = new Date(event.received_at).toLocaleTimeString();
    // Build cells with textContent (not innerHTML): participant names are
    // user-entered, so interpolating them into markup would be an XSS vector.
    const cells = [
      receivedAt,
      event.channel_display ?? event.channel,
      event.bib_number ?? "–",
      event.participant_name ?? "–",
    ];
    for (const value of cells) {
      const td = document.createElement("td");
      td.textContent = value;
      row.appendChild(td);
    }
    body.prepend(row);
    setTimeout(() => row.removeAttribute("id"), 1200);
  });
})();
