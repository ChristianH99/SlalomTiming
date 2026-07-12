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
    row.innerHTML = `
      <td>${receivedAt}</td>
      <td>${event.channel}</td>
      <td>${event.bib_number ?? "–"}</td>
      <td>${event.participant_name ?? "–"}</td>
    `;
    body.prepend(row);
    setTimeout(() => row.removeAttribute("id"), 1200);
  });
})();
