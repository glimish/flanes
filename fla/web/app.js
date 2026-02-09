/* Flanes Web Viewer — Vanilla JS SPA */

const API = '';  // Same origin

// State
let currentView = 'dashboard';
let currentLane = null;
let lanes = [];  // Array of lane objects: [{name, head_state, fork_base, created_at, metadata}]
let lastDataHash = null;
let isFirstLoad = true;

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
  if (el) el.innerHTML = content;
}

function escapeHtml(s) {
  if (s == null) return '';
  const div = document.createElement('div');
  div.textContent = String(s);
  return div.innerHTML;
}

// ── Formatting Helpers ──

function formatTokens(n) {
  if (n == null || n === 0) return '0';
  if (n < 1000) return String(n);
  if (n < 1000000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
  return (n / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
}

function relativeTime(value) {
  if (!value) return '';
  let then;
  if (typeof value === 'number') {
    // Unix epoch seconds (e.g. 1770655661.739)
    then = value * 1000;
  } else {
    // Try ISO string first, then check if it's a numeric string
    const num = Number(value);
    then = isNaN(num) ? new Date(value).getTime() : num * 1000;
  }
  if (isNaN(then)) return '';
  const diff = Math.max(0, Date.now() - then);
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return 'just now';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

function dataChanged(data) {
  const hash = JSON.stringify(data);
  if (hash === lastDataHash) return false;
  lastDataHash = hash;
  return true;
}

function formatDuration(ms) {
  if (ms == null || ms <= 0) return '0s';
  const secs = ms / 1000;
  if (secs < 60) return secs.toFixed(1) + 's';
  const mins = secs / 60;
  if (mins < 60) return mins.toFixed(1) + 'm';
  const hrs = mins / 60;
  return hrs.toFixed(1) + 'h';
}

function safeSubstring(s, len) {
  if (!s || typeof s !== 'string') return '';
  return s.substring(0, len);
}

// ── Views ──

async function showDashboard() {
  currentView = 'dashboard';
  updateNav();

  // Only show loading spinner on first load, not on refresh
  if (isFirstLoad) {
    html('.content', '<div class="loading">Loading...</div>');
  }

  try {
    const [status, laneData, history] = await Promise.all([
      api('/status'),
      api('/lanes'),
      api('/history?limit=20'),
    ]);

    // Anti-flash: skip DOM update if data hasn't changed
    if (!isFirstLoad && !dataChanged({ status, laneData, history })) {
      return;
    }

    // /lanes returns array of objects [{name, head_state, fork_base, ...}]
    lanes = laneData;
    updateSidebar();

    const headHash = safeSubstring(status.current_head, 12) || 'none';
    const laneCount = lanes.length;
    const transCount = history.length;
    const pendingCount = status.pending_proposals || 0;

    // Compute total tokens and wall time from history
    let totalTokens = 0;
    let totalWallMs = 0;
    for (const t of history) {
      if (t.cost) {
        totalTokens += (t.cost.tokens_in || 0) + (t.cost.tokens_out || 0);
        totalWallMs += (t.cost.wall_time_ms || 0);
      }
    }

    // Update header stats
    const headerStats = $('#header-stats');
    if (headerStats) {
      headerStats.innerHTML = `
        <span class="stat"><span class="stat-value">${formatTokens(totalTokens)}</span> tokens</span>
        <span class="stat"><span class="stat-value">${formatDuration(totalWallMs)}</span></span>
        <span class="stat"><span class="stat-value">${laneCount}</span> lanes</span>
      `;
    }

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
          <div class="label">Transitions</div>
          <div class="value">${transCount}</div>
        </div>
        <div class="stat-card">
          <div class="label">Pending</div>
          <div class="value">${pendingCount}</div>
        </div>
        <div class="stat-card">
          <div class="label">Total Tokens</div>
          <div class="value">${formatTokens(totalTokens)}</div>
        </div>
        <div class="stat-card">
          <div class="label">Wall Time</div>
          <div class="value">${formatDuration(totalWallMs)}</div>
        </div>
      </div>

      <div class="card">
        <h2>Recent History</h2>
        ${renderHistoryTable(history)}
      </div>
    `);

    $('.status-pill').textContent = 'Connected';
    $('.status-pill').classList.add('ok');
    isFirstLoad = false;
  } catch (e) {
    html('.content', `<div class="empty">Failed to connect to Flanes API: ${escapeHtml(e.message)}</div>`);
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

    // /lanes returns array of objects — find the matching lane
    const laneObj = laneData.find(l => l.name === lane);
    const headState = laneObj ? laneObj.head_state : null;
    const headHash = safeSubstring(headState, 12) || 'none';
    const forkBase = laneObj ? safeSubstring(laneObj.fork_base, 12) : '';
    const createdAt = laneObj ? laneObj.created_at : '';

    // Compute lane-specific token total and wall time
    let laneTokens = 0;
    let laneWallMs = 0;
    for (const t of history) {
      if (t.cost) {
        laneTokens += (t.cost.tokens_in || 0) + (t.cost.tokens_out || 0);
        laneWallMs += (t.cost.wall_time_ms || 0);
      }
    }

    html('.content', `
      <div class="card">
        <h2>Lane: ${escapeHtml(lane)}</h2>
        <div class="lane-info">
          <div class="lane-info-item">
            <span class="info-label">Head:</span>
            <span class="hash" onclick="showState('${escapeHtml(headState || '')}')">${escapeHtml(headHash)}</span>
          </div>
          ${forkBase ? `<div class="lane-info-item">
            <span class="info-label">Fork base:</span>
            <span class="hash">${escapeHtml(forkBase)}</span>
          </div>` : ''}
          ${createdAt ? `<div class="lane-info-item">
            <span class="info-label">Created:</span>
            <span class="time-relative">${relativeTime(createdAt)}</span>
          </div>` : ''}
          <div class="lane-info-item">
            <span class="info-label">Tokens:</span>
            <span class="cost-inline">${formatTokens(laneTokens)}</span>
          </div>
          <div class="lane-info-item">
            <span class="info-label">Time:</span>
            <span class="cost-inline">${formatDuration(laneWallMs)}</span>
          </div>
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
        <h2>State ${escapeHtml(safeSubstring(stateId, 12))}</h2>
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
          ${escapeHtml(data.size)} bytes | blob: ${escapeHtml(safeSubstring(data.blob_hash, 12))}
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
    // diff.added, diff.removed, diff.modified are dicts {path: hash/details}, NOT arrays
    const added = diff.added || {};
    const removed = diff.removed || {};
    const modified = diff.modified || {};
    const addedPaths = Object.keys(added);
    const removedPaths = Object.keys(removed);
    const modifiedPaths = Object.keys(modified);

    html('.content', `
      <div class="card">
        <h2>Diff</h2>
        <div style="font-size:12px;color:var(--text-muted);margin-bottom:12px">
          ${escapeHtml(safeSubstring(stateA, 12))} &rarr; ${escapeHtml(safeSubstring(stateB, 12))}
        </div>

        ${addedPaths.length ? `<div style="margin-bottom:12px">
          <strong style="color:var(--green)">Added (${addedPaths.length})</strong>
          <ul class="file-tree">
            ${addedPaths.map(f => `<li class="diff-added">
              <div class="diff-file-entry">
                <span>${escapeHtml(f)}</span>
                <span class="diff-hash">${escapeHtml(safeSubstring(added[f], 8))}</span>
              </div>
            </li>`).join('')}
          </ul>
        </div>` : ''}

        ${removedPaths.length ? `<div style="margin-bottom:12px">
          <strong style="color:var(--red)">Removed (${removedPaths.length})</strong>
          <ul class="file-tree">
            ${removedPaths.map(f => `<li class="diff-removed">
              <div class="diff-file-entry">
                <span>${escapeHtml(f)}</span>
                <span class="diff-hash">${escapeHtml(safeSubstring(removed[f], 8))}</span>
              </div>
            </li>`).join('')}
          </ul>
        </div>` : ''}

        ${modifiedPaths.length ? `<div style="margin-bottom:12px">
          <strong style="color:var(--yellow)">Modified (${modifiedPaths.length})</strong>
          <ul class="file-tree">
            ${modifiedPaths.map(f => {
              const m = modified[f];
              const before = safeSubstring(m && m.before, 8);
              const after = safeSubstring(m && m.after, 8);
              return `<li>
                <div class="diff-file-entry">
                  <span>${escapeHtml(f)}</span>
                  <span class="diff-hash">${escapeHtml(before)} &rarr; ${escapeHtml(after)}</span>
                </div>
              </li>`;
            }).join('')}
          </ul>
        </div>` : ''}

        ${diff.unchanged_count ? `<div style="color:var(--text-muted);font-size:12px;margin-top:8px">${diff.unchanged_count} unchanged files</div>` : ''}
        ${!addedPaths.length && !removedPaths.length && !modifiedPaths.length ? '<div class="empty">No changes</div>' : ''}
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
    <div style="overflow-x:auto">
    <table class="history-table">
      <thead>
        <tr>
          <th>Status</th>
          <th>From</th>
          <th>To</th>
          <th>Prompt</th>
          <th>Agent</th>
          <th>Tokens</th>
          <th>Time</th>
          <th>Lane</th>
        </tr>
      </thead>
      <tbody>
        ${history.map(t => {
          const status = t.status || 'proposed';
          const from = safeSubstring(t.from_state, 8);
          const to = safeSubstring(t.to_state, 8);
          const prompt = escapeHtml(safeSubstring(t.intent_prompt, 100));
          const fullPrompt = escapeHtml(t.intent_prompt || '');
          const lane = escapeHtml(t.lane || '');

          // Agent info
          const agent = t.agent || {};
          const agentType = agent.agent_type || '';
          const agentModel = agent.model || '';
          const agentLabel = agentModel ? `${agentType} · ${agentModel}` : agentType;

          // Cost info
          const cost = t.cost || {};
          const totalTokens = (cost.tokens_in || 0) + (cost.tokens_out || 0);

          // Time
          const timeAgo = relativeTime(t.created_at);

          return `<tr>
            <td><span class="badge ${status}">${status}</span></td>
            <td><span class="hash" onclick="showState('${escapeHtml(t.from_state || '')}')">${escapeHtml(from)}</span></td>
            <td><span class="hash" onclick="showState('${escapeHtml(t.to_state || '')}')">${escapeHtml(to)}</span></td>
            <td class="prompt-cell" title="${fullPrompt}">${prompt}</td>
            <td>${agentLabel ? `<span class="agent-badge">${escapeHtml(agentLabel)}</span>` : ''}</td>
            <td><span class="cost-inline">${formatTokens(totalTokens)}</span></td>
            <td><span class="time-relative">${timeAgo}</span></td>
            <td>${lane}</td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>
    </div>
  `;
}

// ── Navigation ──

function updateNav() {
  $$('.sidebar li').forEach(el => el.classList.remove('active'));
  if (currentView === 'dashboard') {
    const dashEl = $('.sidebar li[data-view="dashboard"]');
    if (dashEl) dashEl.classList.add('active');
  } else if (currentView === 'lane' && currentLane) {
    const laneEl = $(`.sidebar li[data-lane="${CSS.escape(currentLane)}"]`);
    if (laneEl) laneEl.classList.add('active');
  }
}

function updateSidebar() {
  const laneList = $('.lane-list');
  if (!laneList) return;

  // lanes is array of objects [{name, head_state, ...}]
  html(laneList, lanes.map(laneObj => {
    const name = laneObj.name || '';
    return `
      <li data-lane="${escapeHtml(name)}" onclick="showLane('${escapeHtml(name)}')">
        <span class="lane-badge"></span>${escapeHtml(name)}
      </li>
    `;
  }).join(''));
}

// ── Init ──

document.addEventListener('DOMContentLoaded', () => {
  showDashboard();

  // Auto-refresh every 5 seconds when on dashboard
  setInterval(() => {
    if (currentView === 'dashboard') showDashboard();
  }, 5000);
});
