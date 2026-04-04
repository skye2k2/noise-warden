let currentSpan = 'day';
async function getJSON(url) {
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    localStorage.setItem('nw_cache_' + url, JSON.stringify({ t: Date.now(), data }));
    return data;
  } catch (e) {
    const cached = localStorage.getItem('nw_cache_' + url);
    if (cached) return JSON.parse(cached).data;
    throw e;
  }
}
async function post(url, body) {
  const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : '{}' });
  return res.json();
}
async function loadStatus() {
  const el = document.getElementById('status'); if (!el) return;
  try {
    const s = await getJSON('/api/status');
    el.innerHTML = `<strong>Armed:</strong> ${s.armed}<br><strong>Emergency Kill:</strong> ${s.emergency_kill}<br><strong>Current dB (slow):</strong> ${s.current_db_slow}<br><strong>Current dB (fast):</strong> ${s.current_db_fast}<br><strong>Classification:</strong> ${s.classification}<br><strong>Incident Active:</strong> ${s.incident_active}<br><strong>Playback Active:</strong> ${s.playback_active}<br><strong>Record Only Now:</strong> ${s.record_only_now}<br><strong>Home Assistant:</strong> ${s.home_assistant_state}<br><strong>Last Error:</strong> ${s.last_error || ''}`;
  } catch { el.textContent = 'Status unavailable (using cached data if present).'; }
}
async function loadIncidents() {
  const tbody = document.querySelector('#incidents tbody'); if (!tbody) return;
  const data = await getJSON('/api/incidents'); tbody.innerHTML = '';
  for (const i of data.items) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${i.id}</td><td>${i.started_at || ''}</td><td>${Number(i.initial_db || 0).toFixed(1)}</td><td>${Number(i.duration_seconds || 0).toFixed(1)}s</td><td>${i.record_only ? 'record-only' : (i.action_taken ? 'responded' : 'logged')}</td><td>${i.snippet_path ? `<audio controls preload="none" src="/api/incidents/${i.id}/audio"></audio>` : ''}</td><td><button onclick="deleteIncident(${i.id})">Delete</button></td>`;
    tbody.appendChild(tr);
  }
}
async function deleteIncident(id) { await post(`/api/incidents/${id}/delete`); await loadIncidents(); if (document.getElementById('timeline')) loadTimeline(); }
async function loadTimeline() {
  const el = document.getElementById('timeline'); if (!el) return;
  const data = await getJSON('/api/timeline?span=' + currentSpan); el.innerHTML = '';
  for (const i of data.items) {
    const div = document.createElement('div'); div.className = 'event';
    div.innerHTML = `<strong>${i.started_at}</strong><br>Initial: ${Number(i.initial_db || 0).toFixed(1)} dB<br>Duration: ${Number(i.duration_seconds || 0).toFixed(1)} s<br>${i.record_only ? 'Night record-only' : 'Day active window'}`;
    el.appendChild(div);
  }
}
function setSpan(span) { currentSpan = span; loadTimeline(); }
async function loadThresholds() { const el = document.getElementById('thresholds'); if (!el) return; el.textContent = JSON.stringify(await getJSON('/api/thresholds'), null, 2); }
async function loadConfig() { const box = document.getElementById('configBox'); if (!box) return; box.value = JSON.stringify(await getJSON('/api/config'), null, 2); }
async function saveConfig() { const box = document.getElementById('configBox'); const payload = JSON.parse(box.value); const result = await post('/api/config/save', payload); alert(result.message || 'Saved'); }
window.addEventListener('DOMContentLoaded', async () => { await loadStatus(); await loadIncidents(); await loadTimeline(); await loadThresholds(); await loadConfig(); setInterval(loadStatus, 5000); });
