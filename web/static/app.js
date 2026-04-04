async function fetchJson(url) {
  const r = await fetch(url);
  return await r.json();
}

async function post(url) {
  const token = prompt("Bearer token (if enabled):", "");
  const headers = token ? { "Authorization": "Bearer " + token } : {};
  const r = await fetch(url, { method: "POST", headers });
  if (!r.ok) alert("Request failed: " + r.status);
  refresh();
}

async function clearIncidents() {
  const token = prompt("Bearer token (if enabled):", "");
  const headers = token ? { "Authorization": "Bearer " + token } : {};
  const r = await fetch("/api/incidents", { method: "DELETE", headers });
  if (!r.ok) alert("Clear failed: " + r.status);
  refresh();
}

function downloadCsv() {
  window.location = "/api/incidents/export";
}

function renderStatus(s) {
  document.getElementById("status").innerHTML = `
    <table>
      <tr><th>Armed</th><td>${s.armed}</td></tr>
      <tr><th>Manual Kill</th><td>${s.manual_kill}</td></tr>
      <tr><th>Response Active</th><td>${s.response_active}</td></tr>
      <tr><th>Current dB</th><td>${Number(s.current_db).toFixed(1)}</td></tr>
      <tr><th>Slow dB</th><td>${Number(s.current_slow_db).toFixed(1)}</td></tr>
      <tr><th>Fast dB</th><td>${Number(s.current_fast_db).toFixed(1)}</td></tr>
      <tr><th>Classification</th><td>${s.last_classification}</td></tr>
      <tr><th>Active Incident ID</th><td>${s.active_incident_id ?? ""}</td></tr>
      <tr><th>Updated</th><td>${s.last_update ?? ""}</td></tr>
    </table>
  `;
}

function renderIncidents(rows) {
  let html = "<table><tr><th>ID</th><th>Start</th><th>End</th><th>Period</th><th>Peak</th><th>Threshold</th><th>Mode</th></tr>";
  for (const r of rows) {
    html += `<tr>
      <td>${r.id}</td>
      <td>${r.started_at || ""}</td>
      <td>${r.ended_at || ""}</td>
      <td>${r.day_or_night || ""}</td>
      <td>${Number(r.peak_db).toFixed(1)}</td>
      <td>${Number(r.threshold_db).toFixed(1)}</td>
      <td>${r.mode || ""}</td>
    </tr>`;
  }
  html += "</table>";
  document.getElementById("incidents").innerHTML = html;
}

function renderStateLog(rows) {
  let html = "<table><tr><th>Time</th><th>Key</th><th>Value</th></tr>";
  for (const r of rows) {
    html += `<tr><td>${r.ts}</td><td>${r.key}</td><td>${r.value}</td></tr>`;
  }
  html += "</table>";
  document.getElementById("stateLog").innerHTML = html;
}

async function refresh() {
  const [status, incidents, stateLog] = await Promise.all([
    fetchJson("/api/status"),
    fetchJson("/api/incidents?limit=50"),
    fetchJson("/api/state-log?limit=50"),
  ]);
  renderStatus(status);
  renderIncidents(incidents);
  renderStateLog(stateLog);
}

refresh();
setInterval(refresh, 3000);
