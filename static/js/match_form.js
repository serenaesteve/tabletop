document.addEventListener("DOMContentLoaded", () => {
  const usernames = {};
  document.querySelectorAll(".player-toggle").forEach((checkbox) => {
    usernames[checkbox.dataset.idx] = checkbox.closest("label").textContent.trim();

    checkbox.addEventListener("change", () => {
      const idx = checkbox.dataset.idx;
      const rankInput = document.querySelector(`.rank-input[data-idx="${idx}"]`);
      const scoreInput = document.querySelector(`.score-input[data-idx="${idx}"]`);
      const idHolder = document.querySelector(`.user-id-holder[data-idx="${idx}"]`);
      const enabled = checkbox.checked;
      rankInput.disabled = !enabled;
      scoreInput.disabled = !enabled;
      idHolder.disabled = !enabled;
      rankInput.required = enabled;
    });
  });

  const form = document.getElementById("match-form");
  if (!form) return;

  form.addEventListener("submit", (e) => {
    const rows = [];
    document.querySelectorAll(".player-toggle:checked").forEach((checkbox) => {
      const idx = checkbox.dataset.idx;
      const rank = document.querySelector(`.rank-input[data-idx="${idx}"]`).value || "?";
      rows.push({ rank: parseInt(rank, 10) || 999, name: usernames[idx] });
    });

    if (rows.length < 2) return; // el backend ya valida el minimo

    rows.sort((a, b) => a.rank - b.rank);
    const summary = rows.map((r) => `${r.rank}º ${r.name}`).join("\n");
    const ok = window.confirm(`¿Confirmas esta partida?\n\n${summary}`);
    if (!ok) e.preventDefault();
  });
});
