(function () {
  var el = document.getElementById("chart-data");
  if (!el || typeof Chart === "undefined") return;
  var data = JSON.parse(el.textContent);
  var opts = { plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, ticks: { precision: 0 } } } };
  new Chart(document.getElementById("sourceChart"), {
    type: "bar",
    data: { labels: data.source.labels, datasets: [{ data: data.source.values, backgroundColor: "#ea580c" }] },
    options: opts
  });
  new Chart(document.getElementById("monthChart"), {
    type: "line",
    data: { labels: data.month.labels, datasets: [{ data: data.month.values, borderColor: "#ea580c", backgroundColor: "rgba(234,88,12,.15)", fill: true, tension: 0.3 }] },
    options: opts
  });
})();
