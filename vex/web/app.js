/* Vex Web Viewer — Vanilla JS SPA */

const API = '';  // Same origin

// State
let currentView = 'dashboard';
let currentLane = null;
let lanes = [];

// API helpers
async function api(path) {
  const resp = await fetch(`${API}${path}`);
  if (!resp.ok) throw new Error(`API error: ${resp.status}`);
  return resp.json();
}

// DOM helpers
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

function html(el, content) {
  if (typeof el === 'string') el = $(el);
  el.innerHTML = content;
}

function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s;
  return div.innerHTML;
}

// ── Views ──

async function showDashboard() {
  currentView = 'dashboard';
  updateNav();

  html('.content', '<div class="loading">Loading...</div>');

  try {
    const [status, laneData, history] = await Promise.all([
      api('/status'),
      api('/lanes'),
      api('/history?limit=10'),
    ]);

    lanes = Object.keys(laneData);
    updateSidebar();

    const headHash = status.head ? status.head.substring(0, 12) : 'none';
    const laneCount = lanes.length;
    const transCount = history.length;

    html('.content', `
      <div class="status-grid">
        <div class="stat-card">
          <div class="label">Current Head</div>
          <div class="value">${escapeHtml(headHash)}</div>
        </div>
        <div class="stat-card">
          <div class="label">Lanes</div>
          <div class="value">${laneCount}</div>
        </div>
        <div class="stat-card">
          <div class="label">Recent Transitions</div>
          <div class="value">${transCount}</div>
        </div>
        <div class="stat-card">
          <div class="label">Default Lane</div>
          <div class="value" style="font-size:16px">${escapeHtml(status.default_lane || 'main')}</div>
        </div>
      </div>

      <div class="card">
        <h2>Recent History</h2>
        ${renderHistoryTable(history)}
      </div>
    `);

    $('.status-pill').textContent = 'Connected';
    $('.status-pill').classList.add('ok');
  } catch (e) {
    html('.content', `<div class="empty">Failed to connect to Vex API: ${escapeHtml(e.message)}</div>`);
    $('.status-pill').textContent = 'Disconnected';
    $('.status-pill').classList.remove('ok');
  }
}

async function showLane(lane) {
  currentView = 'lane';
  currentLane = lane;
  updateNav();

  html('.content', '<div class="loading">Loading...</div>');

  try {
    const [laneData, history] = await Promise.all([
      api('/lanes'),
      api(`/history?lane=${encodeURIComponent(lane)}&limit=50`),
    ]);

    const head = laneData[lane];
    const headHash = head ? head.substring(0, 12) : 'none';

    html('.content', `
      <div class="card">
        <h2>Lane: ${escapeHtml(lane)}</h2>
        <div style="margin-bottom:12px">
          <span class="label" style="color:var(--text-muted);font-size:12px">Head: </span>
          <span class="hash" onclick="showState('${escapeHtml(head)}')">${escapeHtml(headHash)}</span>
        </div>
      </div>

      <div class="card">
        <h2>History (${history.length} transitions)</h2>
        ${renderHistoryTable(history)}
      </div>
    `);
  } catch (e) {
    html('.content', `<div class="empty">Error: ${escapeHtml(e.message)}</div>`);
  }
}

async function showState(stateId) {
  if (!stateId) return;
  currentView = 'state';
  updateNav();

  html('.content', '<div class="loading">Loading state...</div>');

  try {
    const [state, files] = await Promise.all([
      api(`/states/${stateId}`),
      api(`/states/${stateId}/files`),
    ]);

    const fileList = files.files || [];

    html('.content', `
      <div class="card">
        <h2>State ${escapeHtml(stateId.substring(0, 12))}</h2>
        <div style="font-size:12px;color:var(--text-muted);font-family:var(--font-mono)">
          ${escapeHtml(stateId)}
        </div>
      </div>

      <div class="card">
        <h2>Files (${fileList.length})</h2>
        <ul class="file-tree">
          ${fileList.map(f => `<li onclick="showFile('${escapeHtml(stateId)}','${escapeHtml(f)}')">${escapeHtml(f)}</li>`).join('')}
        </ul>
        ${fileList.length === 0 ? '<div class="empty">No files</div>' : ''}
      </div>
    `);
  } catch (e) {
    html('.content', `<div class="empty">Error: ${escapeHtml(e.message)}</div>`);
  }
}

async function showFile(stateId, filePath) {
  currentView = 'file';
  updateNav();

  html('.content', '<div class="loading">Loading file...</div>');

  try {
    const data = await api(`/states/${stateId}/files/${filePath}`);
    let content;
    try {
      content = atob(data.content_base64);
    } catch {
      content = '(binary content)';
    }

    html('.content', `
      <div class="card">
        <h2>${escapeHtml(filePath)}</h2>
        <div style="font-size:12px;color:var(--text-muted);margin-bottom:8px">
          ${escapeHtml(data.size)} bytes | blob: ${escapeHtml(data.blob_hash.substring(0, 12))}
        </div>
        <pre class="diff-view">${escapeHtml(content)}</pre>
      </div>
    `);
  } catch (e) {
    html('.content', `<div class="empty">Error: ${escapeHtml(e.message)}</div>`);
  }
}

async function showDiff(stateA, stateB) {
  currentView = 'diff';
  updateNav();

  html('.content', '<div class="loading">Computing diff...</div>');

  try {
    const diff = await api(`/diff?a=${stateA}&b=${stateB}`);
    const added = diff.added || [];
    const removed = diff.removed || [];
    const modified = diff.modified || [];

    html('.content', `
      <div class="card">
        <h2>Diff</h2>
        <div style="font-size:12px;color:var(--text-muted);margin-bottom:12px">
          ${escapeHtml(stateA.substring(0, 12))} &rarr; ${escapeHtml(stateB.substring(0, 12))}
        </div>

        ${added.length ? `<div style="margin-bottom:8px"><strong style="color:var(--green)">Added (${added.length})</strong><ul class="file-tree">${added.map(f => `<li class="diff-added">${escapeHtml(f)}</li>`).join('')}</ul></div>` : ''}
        ${removed.length ? `<div style="margin-bottom:8px"><strong style="color:var(--red)">Removed (${removed.length})</strong><ul class="file-tree">${removed.map(f => `<li class="diff-removed">${escapeHtml(f)}</li>`).join('')}</ul></div>` : ''}
        ${modified.length ? `<div style="margin-bottom:8px"><strong style="color:var(--yellow)">Modified (${modified.length})</strong><ul class="file-tree">${modified.map(f => `<li>${escapeHtml(f)}</li>`).join('')}</ul></div>` : ''}
        ${!added.length && !removed.length && !modified.length ? '<div class="empty">No changes</div>' : ''}
      </div>
    `);
  } catch (e) {
    html('.content', `<div class="empty">Error: ${escapeHtml(e.message)}</div>`);
  }
}

// ── Renderers ──

function renderHistoryTable(history) {
  if (!history.length) return '<div class="empty">No transitions</div>';

  return `
    <table class="history-table">
      <thead>
        <tr>
          <th>Status</th>
          <th>ID</th>
          <th>From</th>
          <th>To</th>
          <th>Prompt</th>
          <th>Lane</th>
        </tr>
      </thead>
      <tbody>
        ${history.map(t => {
          const status = t.status || 'proposed';
          const tid = (t.id || '').substring(0, 8);
          const from = (t.from_state || '').substring(0, 8);
          const to = (t.to_state || '').substring(0, 8);
          const prompt = escapeHtml((t.prompt || '').substring(0, 60));
          const lane = escapeHtml(t.lane || '');

          return `<tr>
            <td><span class="badge ${status}">${status}</span></td>
            <td><span class="hash">${escapeHtml(tid)}</span></td>
            <td><span class="hash" onclick="showState('${escapeHtml(t.from_state || '')}')">${escapeHtml(from)}</span></td>
            <td><span class="hash" onclick="showState('${escapeHtml(t.to_state || '')}')">${escapeHtml(to)}</span></td>
            <td>${prompt}</td>
            <td>${lane}</td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>
  `;
}

// ── Navigation ──

function updateNav() {
  $$('.sidebar li').forEach(el => el.classList.remove('active'));
  if (currentView === 'dashboard') {
    const dashEl = $('.sidebar li[data-view="dashboard"]');
    if (dashEl) dashEl.classList.add('active');
  } else if (currentView === 'lane' && currentLane) {
    const laneEl = $(`.sidebar li[data-lane="${currentLane}"]`);
    if (laneEl) laneEl.classList.add('active');
  }
}

function updateSidebar() {
  const laneList = $('.lane-list');
  if (!laneList) return;

  html(laneList, lanes.map(lane => `
    <li data-lane="${escapeHtml(lane)}" onclick="showLane('${escapeHtml(lane)}')">
      <span class="lane-badge"></span>${escapeHtml(lane)}
    </li>
  `).join(''));
}

// ── Init ──

document.addEventListener('DOMContentLoaded', () => {
  showDashboard();

  // Auto-refresh every 5 seconds when on dashboard
  setInterval(() => {
    if (currentView === 'dashboard') showDashboard();
  }, 5000);
});
