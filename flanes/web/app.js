/* Flanes Web Viewer — Vanilla JS SPA */

// ── State ──
let currentView = 'dashboard';
let currentLane = null;
let lanes = [];
let lastDataHash = null;
let isFirstLoad = true;

// ── API ──
async function api(path) {
  const resp = await fetch(path);
  if (!resp.ok) throw new Error(`API error: ${resp.status}`);
  return resp.json();
}

// ── DOM Helpers ──
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

function setHtml(el, content) {
  if (typeof el === 'string') el = $(el);
  if (el) el.innerHTML = content;
}

function escapeHtml(s) {
  if (s == null) return '';
  const div = document.createElement('div');
  div.textContent = String(s);
  return div.innerHTML;
}

// ── Formatting ──
function formatTokens(n) {
  if (n == null || n === 0) return '0';
  if (n < 1000) return String(n);
  if (n < 1000000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
  return (n / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
}

function relativeTime(value) {
  if (!value) return '';
  let then;
  if (typeof value === 'number') then = value * 1000;
  else {
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

function formatDuration(ms) {
  if (ms == null || ms <= 0) return '0s';
  const secs = ms / 1000;
  if (secs < 60) return secs.toFixed(1) + 's';
  const mins = secs / 60;
  if (mins < 60) return mins.toFixed(1) + 'm';
  return (mins / 60).toFixed(1) + 'h';
}

function safeSubstring(s, len) {
  if (!s || typeof s !== 'string') return '';
  return s.substring(0, len);
}

function dataChanged(data) {
  const hash = JSON.stringify(data);
  if (hash === lastDataHash) return false;
  lastDataHash = hash;
  return true;
}

function decodeBase64(b64) {
  try {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new TextDecoder('utf-8', { fatal: false }).decode(bytes);
  } catch { return null; }
}

function isBinaryContent(text) {
  if (!text) return false;
  const check = text.substring(0, 8192);
  return check.indexOf('\0') !== -1;
}

// ── Tooltip ──
function showTooltip(x, y, content) {
  const tt = $('#tooltip');
  tt.innerHTML = content;
  tt.classList.remove('hidden');
  // Position: offset from cursor, keep on screen
  const pad = 12;
  let left = x + pad;
  let top = y + pad;
  const rect = tt.getBoundingClientRect();
  if (left + rect.width > window.innerWidth) left = x - rect.width - pad;
  if (top + rect.height > window.innerHeight) top = y - rect.height - pad;
  tt.style.left = Math.max(0, left) + 'px';
  tt.style.top = Math.max(0, top) + 'px';
}

function hideTooltip() {
  const tt = $('#tooltip');
  tt.classList.add('hidden');
}

// ── Router ──
function navigate(hash) {
  window.location.hash = hash;
}

function resolveRoute() {
  const hash = window.location.hash || '#/dashboard';

  if (hash === '#/dashboard' || hash === '#/' || hash === '') {
    lastDataHash = null;
    showDashboard();
  } else if (hash === '#/timeline') {
    showTimeline();
  } else if (hash === '#/workspaces') {
    showWorkspaces();
  } else if (hash.startsWith('#/lane/')) {
    showLane(decodeURIComponent(hash.substring(7)));
  } else if (hash.startsWith('#/state/')) {
    showState(hash.substring(8));
  } else if (hash.startsWith('#/trace/')) {
    showTrace(hash.substring(8));
  } else if (hash.startsWith('#/diff/')) {
    const parts = hash.substring(7).split('/');
    if (parts.length === 2) showDiff(parts[0], parts[1]);
  } else if (hash.startsWith('#/search')) {
    const q = new URLSearchParams(hash.split('?')[1] || '').get('q') || '';
    showSearch(q);
  } else {
    showDashboard();
  }
}

// ── Navigation UI ──
function updateNav() {
  $$('.sidebar li').forEach(el => el.classList.remove('active'));
  if (currentView === 'dashboard') {
    const el = $('[data-view="dashboard"]');
    if (el) el.classList.add('active');
  } else if (currentView === 'timeline') {
    const el = $('[data-view="timeline"]');
    if (el) el.classList.add('active');
  } else if (currentView === 'workspaces') {
    const el = $('[data-view="workspaces"]');
    if (el) el.classList.add('active');
  } else if (currentView === 'lane' && currentLane) {
    const el = $(`[data-lane="${CSS.escape(currentLane)}"]`);
    if (el) el.classList.add('active');
  }
}

function updateSidebar() {
  const laneList = $('.lane-list');
  if (!laneList) return;
  setHtml(laneList, lanes.map(l => {
    const name = l.name || '';
    return `<li data-lane="${escapeHtml(name)}" onclick="navigate('#/lane/${encodeURIComponent(name)}')">
      <span class="lane-badge"></span>${escapeHtml(name)}
    </li>`;
  }).join(''));
}

// ── Shared Renderers ──
function renderHistoryTable(history) {
  if (!history.length) return '<div class="empty">No transitions</div>';
  return `
    <div style="overflow-x:auto">
    <table class="history-table">
      <thead><tr>
        <th>Status</th><th>From</th><th>To</th><th>Prompt</th>
        <th>Agent</th><th>Tokens</th><th>Time</th><th>Lane</th>
      </tr></thead>
      <tbody>
        ${history.map(t => {
          const status = t.status || 'proposed';
          const from = safeSubstring(t.from_state, 8);
          const to = safeSubstring(t.to_state, 8);
          const prompt = escapeHtml(safeSubstring(t.intent_prompt, 100));
          const fullPrompt = escapeHtml(t.intent_prompt || '');
          const lane = escapeHtml(t.lane || '');
          const agent = t.agent || {};
          const agentType = agent.agent_type || '';
          const agentModel = agent.model || '';
          const agentLabel = agentModel ? `${agentType} · ${agentModel}` : agentType;
          const cost = t.cost || {};
          const totalTokens = (cost.tokens_in || 0) + (cost.tokens_out || 0);
          const timeAgo = relativeTime(t.created_at);
          const diffLink = t.from_state && t.to_state
            ? `onclick="navigate('#/diff/${t.from_state}/${t.to_state}')"`
            : `onclick="navigate('#/state/${t.to_state || ''}')"`;
          return `<tr style="cursor:pointer" ${diffLink}>
            <td><span class="badge ${status}">${status}</span></td>
            <td><span class="hash">${escapeHtml(from)}</span></td>
            <td><span class="hash">${escapeHtml(to)}</span></td>
            <td class="prompt-cell" title="${fullPrompt}">${prompt}</td>
            <td>${agentLabel ? `<span class="agent-badge">${escapeHtml(agentLabel)}</span>` : ''}</td>
            <td><span class="cost-inline">${formatTokens(totalTokens)}</span></td>
            <td><span class="time-relative">${timeAgo}</span></td>
            <td>${lane}</td>
          </tr>`;
        }).join('')}
      </tbody>
    </table></div>`;
}

// ══════════════════════════════════════════════════════
// VIEW: Dashboard
// ══════════════════════════════════════════════════════
async function showDashboard() {
  currentView = 'dashboard';
  updateNav();

  if (isFirstLoad) setHtml('.content', '<div class="loading">Loading...</div>');

  try {
    const [status, laneData] = await Promise.all([api('/status'), api('/lanes')]);
    const allLaneHistories = await Promise.all(
      laneData.map(l => api(`/history?lane=${encodeURIComponent(l.name)}&limit=200`).catch(() => []))
    );

    if (!isFirstLoad && !dataChanged({ status, laneData, allLaneHistories })) return;

    lanes = laneData;
    updateSidebar();

    const headHash = safeSubstring(status.current_head, 12) || 'none';
    let totalTokens = 0, totalWallMs = 0, totalTransitions = 0;
    const allTransitions = [];

    for (const lh of allLaneHistories) {
      for (const t of lh) {
        totalTransitions++;
        allTransitions.push(t);
        if (t.cost) {
          totalTokens += (t.cost.tokens_in || 0) + (t.cost.tokens_out || 0);
          totalWallMs += (t.cost.wall_time_ms || 0);
        }
      }
    }

    allTransitions.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    const recent = allTransitions.slice(0, 20);

    const headerStats = $('#header-stats');
    if (headerStats) {
      headerStats.innerHTML = `
        <span class="stat"><span class="stat-value">${formatTokens(totalTokens)}</span> tokens</span>
        <span class="stat"><span class="stat-value">${formatDuration(totalWallMs)}</span></span>
        <span class="stat"><span class="stat-value">${lanes.length}</span> lanes</span>`;
    }

    setHtml('.content', `
      <div class="status-grid">
        <div class="stat-card"><div class="label">Current Head</div><div class="value small">${escapeHtml(headHash)}</div></div>
        <div class="stat-card"><div class="label">Lanes</div><div class="value">${lanes.length}</div></div>
        <div class="stat-card"><div class="label">Transitions</div><div class="value">${totalTransitions}</div></div>
        <div class="stat-card"><div class="label">Pending</div><div class="value">${status.pending_proposals || 0}</div></div>
        <div class="stat-card"><div class="label">Total Tokens</div><div class="value">${formatTokens(totalTokens)}</div></div>
        <div class="stat-card"><div class="label">Wall Time</div><div class="value">${formatDuration(totalWallMs)}</div></div>
      </div>
      <div class="card"><h2>Recent History</h2>${renderHistoryTable(recent)}</div>`);

    $('.status-pill').textContent = 'Connected';
    $('.status-pill').classList.add('ok');
    isFirstLoad = false;
  } catch (e) {
    setHtml('.content', `<div class="empty">Failed to connect: ${escapeHtml(e.message)}</div>`);
    $('.status-pill').textContent = 'Disconnected';
    $('.status-pill').classList.remove('ok');
  }
}

// ══════════════════════════════════════════════════════
// VIEW: Lane
// ══════════════════════════════════════════════════════
async function showLane(lane) {
  currentView = 'lane';
  currentLane = lane;
  updateNav();
  setHtml('.content', '<div class="loading">Loading...</div>');

  try {
    const laneData = await api('/lanes');
    const laneObj = laneData.find(l => l.name === lane);
    const headState = laneObj ? laneObj.head_state : null;
    const headHash = safeSubstring(headState, 12) || 'none';
    const forkBase = laneObj ? safeSubstring(laneObj.fork_base, 12) : '';
    const createdAt = laneObj ? laneObj.created_at : '';
    const isMainLane = !laneObj || !laneObj.fork_base;

    let history, laneTokens = 0, laneWallMs = 0;
    if (isMainLane) {
      const all = await Promise.all(laneData.map(l => api(`/history?lane=${encodeURIComponent(l.name)}&limit=200`).catch(() => [])));
      history = [];
      for (const lh of all) for (const t of lh) {
        history.push(t);
        if (t.cost) { laneTokens += (t.cost.tokens_in || 0) + (t.cost.tokens_out || 0); laneWallMs += (t.cost.wall_time_ms || 0); }
      }
      history.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    } else {
      history = await api(`/history?lane=${encodeURIComponent(lane)}&limit=50`);
      for (const t of history) if (t.cost) {
        laneTokens += (t.cost.tokens_in || 0) + (t.cost.tokens_out || 0);
        laneWallMs += (t.cost.wall_time_ms || 0);
      }
    }

    setHtml('.content', `
      <div class="card">
        <h2>Lane: ${escapeHtml(lane)}</h2>
        <div class="lane-info">
          <div class="lane-info-item"><span class="info-label">Head:</span>
            <span class="hash" onclick="navigate('#/state/${escapeHtml(headState || '')}')">${escapeHtml(headHash)}</span></div>
          ${forkBase ? `<div class="lane-info-item"><span class="info-label">Fork base:</span><span class="hash">${escapeHtml(forkBase)}</span></div>` : ''}
          ${createdAt ? `<div class="lane-info-item"><span class="info-label">Created:</span><span class="time-relative">${relativeTime(createdAt)}</span></div>` : ''}
          <div class="lane-info-item"><span class="info-label">Tokens:</span><span class="cost-inline">${formatTokens(laneTokens)}</span></div>
          <div class="lane-info-item"><span class="info-label">Time:</span><span class="cost-inline">${formatDuration(laneWallMs)}</span></div>
        </div>
      </div>
      <div class="card"><h2>History (${history.length} transitions)</h2>${renderHistoryTable(history)}</div>`);
  } catch (e) {
    setHtml('.content', `<div class="empty">Error: ${escapeHtml(e.message)}</div>`);
  }
}

// ══════════════════════════════════════════════════════
// VIEW: State
// ══════════════════════════════════════════════════════
async function showState(stateId) {
  if (!stateId) return;
  currentView = 'state';
  updateNav();
  setHtml('.content', '<div class="loading">Loading state...</div>');

  try {
    const [state, files] = await Promise.all([api(`/states/${stateId}`), api(`/states/${stateId}/files`)]);
    const fileList = files.files || [];

    setHtml('.content', `
      <div class="card">
        <h2>State ${escapeHtml(safeSubstring(stateId, 12))}</h2>
        <div style="font-size:12px;color:var(--text-muted);font-family:var(--font-mono);margin-bottom:12px">${escapeHtml(stateId)}</div>
        <button class="btn btn-accent" onclick="navigate('#/trace/${escapeHtml(stateId)}')">Show Lineage</button>
      </div>
      <div class="card">
        <h2>Files (${fileList.length})</h2>
        <ul class="file-tree">
          ${fileList.map(f => `<li onclick="showFile('${escapeHtml(stateId)}','${escapeHtml(f)}')">${escapeHtml(f)}</li>`).join('')}
        </ul>
        ${fileList.length === 0 ? '<div class="empty">No files</div>' : ''}
      </div>`);
  } catch (e) {
    setHtml('.content', `<div class="empty">Error: ${escapeHtml(e.message)}</div>`);
  }
}

// ══════════════════════════════════════════════════════
// VIEW: File
// ══════════════════════════════════════════════════════
async function showFile(stateId, filePath) {
  currentView = 'file';
  updateNav();
  setHtml('.content', '<div class="loading">Loading file...</div>');

  try {
    const data = await api(`/states/${stateId}/files/${filePath}`);
    const content = decodeBase64(data.content_base64) || '(binary content)';

    setHtml('.content', `
      <div class="card">
        <h2>${escapeHtml(filePath)}</h2>
        <div style="font-size:12px;color:var(--text-muted);margin-bottom:8px">
          ${escapeHtml(data.size)} bytes | blob: ${escapeHtml(safeSubstring(data.blob_hash, 12))}
        </div>
        <pre class="diff-view">${escapeHtml(content)}</pre>
      </div>`);
  } catch (e) {
    setHtml('.content', `<div class="empty">Error: ${escapeHtml(e.message)}</div>`);
  }
}

// ══════════════════════════════════════════════════════
// VIEW: Diff (with inline content diffs)
// ══════════════════════════════════════════════════════
async function showDiff(stateA, stateB) {
  currentView = 'diff';
  updateNav();
  setHtml('.content', '<div class="loading">Computing diff...</div>');

  try {
    const diff = await api(`/diff?a=${stateA}&b=${stateB}`);
    const added = diff.added || {};
    const removed = diff.removed || {};
    const modified = diff.modified || {};
    const addedPaths = Object.keys(added);
    const removedPaths = Object.keys(removed);
    const modifiedPaths = Object.keys(modified);

    let sections = '';

    // Added files
    for (const path of addedPaths) {
      sections += renderDiffFileSection(path, 'added', null, added[path], stateB);
    }
    // Removed files
    for (const path of removedPaths) {
      sections += renderDiffFileSection(path, 'removed', removed[path], null, stateA);
    }
    // Modified files
    for (const path of modifiedPaths) {
      const m = modified[path];
      sections += renderDiffFileSection(path, 'modified', m.before, m.after, null);
    }

    setHtml('.content', `
      <div class="card">
        <h2>Diff</h2>
        <div style="font-size:12px;color:var(--text-muted);margin-bottom:12px">
          ${escapeHtml(safeSubstring(stateA, 12))} &rarr; ${escapeHtml(safeSubstring(stateB, 12))}
          &nbsp;|&nbsp; <span style="color:var(--green)">+${addedPaths.length}</span>
          <span style="color:var(--red)">-${removedPaths.length}</span>
          <span style="color:var(--yellow)">~${modifiedPaths.length}</span>
          ${diff.unchanged_count ? `<span style="color:var(--text-muted)">(${diff.unchanged_count} unchanged)</span>` : ''}
        </div>
        ${sections || '<div class="empty">No changes</div>'}
      </div>`);

    // Wire up expand/collapse
    document.querySelectorAll('.diff-file-header').forEach(header => {
      header.addEventListener('click', () => toggleDiffSection(header));
    });
  } catch (e) {
    setHtml('.content', `<div class="empty">Error: ${escapeHtml(e.message)}</div>`);
  }
}

function renderDiffFileSection(path, type, beforeHash, afterHash, fallbackState) {
  const id = `diff-${btoa(path).replace(/[^a-zA-Z0-9]/g, '')}`;
  return `
    <div class="diff-file-section" id="${id}"
         data-type="${type}" data-before="${beforeHash || ''}" data-after="${afterHash || ''}" data-state="${fallbackState || ''}" data-path="${escapeHtml(path)}">
      <div class="diff-file-header">
        <span class="diff-file-icon">&#9654;</span>
        <span class="diff-file-badge ${type}">${type}</span>
        <span>${escapeHtml(path)}</span>
      </div>
      <div class="diff-file-body">
        <div class="diff-loading">Click to load diff...</div>
      </div>
    </div>`;
}

async function toggleDiffSection(header) {
  const section = header.closest('.diff-file-section');
  const body = section.querySelector('.diff-file-body');
  const icon = header.querySelector('.diff-file-icon');
  const isExpanded = body.classList.contains('expanded');

  if (isExpanded) {
    body.classList.remove('expanded');
    icon.classList.remove('expanded');
    return;
  }

  body.classList.add('expanded');
  icon.classList.add('expanded');

  // Only fetch once
  if (body.dataset.loaded) return;
  body.dataset.loaded = 'true';
  body.innerHTML = '<div class="diff-loading">Loading...</div>';

  const type = section.dataset.type;
  const beforeHash = section.dataset.before;
  const afterHash = section.dataset.after;

  try {
    if (type === 'added') {
      const obj = await api(`/objects/${afterHash}`);
      const text = decodeBase64(obj.content_base64);
      if (!text || isBinaryContent(text)) {
        body.innerHTML = '<div class="diff-loading">(binary file)</div>';
      } else {
        body.innerHTML = renderAllLinesAs(text, 'added');
      }
    } else if (type === 'removed') {
      const obj = await api(`/objects/${beforeHash}`);
      const text = decodeBase64(obj.content_base64);
      if (!text || isBinaryContent(text)) {
        body.innerHTML = '<div class="diff-loading">(binary file)</div>';
      } else {
        body.innerHTML = renderAllLinesAs(text, 'removed');
      }
    } else {
      // Modified: fetch both, compute diff
      const [objA, objB] = await Promise.all([api(`/objects/${beforeHash}`), api(`/objects/${afterHash}`)]);
      const textA = decodeBase64(objA.content_base64);
      const textB = decodeBase64(objB.content_base64);
      if (!textA || !textB || isBinaryContent(textA) || isBinaryContent(textB)) {
        body.innerHTML = '<div class="diff-loading">(binary file)</div>';
      } else {
        const hunks = computeDiff(textA, textB);
        body.innerHTML = renderDiffHunks(hunks);
      }
    }
  } catch (e) {
    body.innerHTML = `<div class="diff-loading">Error loading: ${escapeHtml(e.message)}</div>`;
  }
}

function renderAllLinesAs(text, type) {
  const lines = text.split('\n');
  const prefix = type === 'added' ? '+' : '-';
  return lines.map((line, i) => {
    const num = i + 1;
    const numA = type === 'removed' ? num : '';
    const numB = type === 'added' ? num : '';
    return `<div class="diff-line ${type}">
      <span class="diff-line-number">${numA}</span>
      <span class="diff-line-number">${numB}</span>
      <span class="diff-line-content">${prefix}${escapeHtml(line)}</span>
    </div>`;
  }).join('');
}

// ── LCS Diff Algorithm ──
function computeDiff(oldText, newText) {
  const oldLines = oldText.split('\n');
  const newLines = newText.split('\n');

  // Trim common prefix
  let prefixLen = 0;
  while (prefixLen < oldLines.length && prefixLen < newLines.length && oldLines[prefixLen] === newLines[prefixLen]) prefixLen++;

  // Trim common suffix
  let suffixLen = 0;
  while (suffixLen < (oldLines.length - prefixLen) && suffixLen < (newLines.length - prefixLen)
         && oldLines[oldLines.length - 1 - suffixLen] === newLines[newLines.length - 1 - suffixLen]) suffixLen++;

  const oldTrimmed = oldLines.slice(prefixLen, oldLines.length - suffixLen);
  const newTrimmed = newLines.slice(prefixLen, newLines.length - suffixLen);

  // For very large diffs, show simplified view
  if (oldTrimmed.length > 3000 || newTrimmed.length > 3000) {
    return [{ oldStart: prefixLen + 1, oldCount: oldTrimmed.length, newStart: prefixLen + 1, newCount: newTrimmed.length,
      lines: oldTrimmed.map(l => ({ type: 'remove', text: l })).concat(newTrimmed.map(l => ({ type: 'add', text: l }))) }];
  }

  // LCS via 2-row DP
  const m = oldTrimmed.length, n = newTrimmed.length;
  let prev = new Uint16Array(n + 1);
  let curr = new Uint16Array(n + 1);

  for (let i = 1; i <= m; i++) {
    [prev, curr] = [curr, prev];
    curr.fill(0);
    for (let j = 1; j <= n; j++) {
      if (oldTrimmed[i - 1] === newTrimmed[j - 1]) curr[j] = prev[j - 1] + 1;
      else curr[j] = Math.max(prev[j], curr[j - 1]);
    }
  }

  // We need full table for backtracking — rebuild with full matrix but use arrays
  // Actually, let's use a more memory-friendly backtrack approach
  const edits = [];
  let i = m, j = n;

  // Rebuild full LCS table (needed for backtrack)
  const dp = [];
  for (let r = 0; r <= m; r++) dp[r] = new Uint16Array(n + 1);
  for (let r = 1; r <= m; r++) {
    for (let c = 1; c <= n; c++) {
      if (oldTrimmed[r - 1] === newTrimmed[c - 1]) dp[r][c] = dp[r - 1][c - 1] + 1;
      else dp[r][c] = Math.max(dp[r - 1][c], dp[r][c - 1]);
    }
  }

  // Backtrack
  i = m; j = n;
  const editOps = [];
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && oldTrimmed[i - 1] === newTrimmed[j - 1]) {
      editOps.push({ type: 'equal', oldIdx: prefixLen + i - 1, newIdx: prefixLen + j - 1, text: oldTrimmed[i - 1] });
      i--; j--;
    } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
      editOps.push({ type: 'add', newIdx: prefixLen + j - 1, text: newTrimmed[j - 1] });
      j--;
    } else {
      editOps.push({ type: 'remove', oldIdx: prefixLen + i - 1, text: oldTrimmed[i - 1] });
      i--;
    }
  }
  editOps.reverse();

  // Build full edit list including prefix and suffix
  const fullEdits = [];
  for (let k = 0; k < prefixLen; k++) fullEdits.push({ type: 'equal', oldIdx: k, newIdx: k, text: oldLines[k] });
  for (const op of editOps) fullEdits.push(op);
  const suffStart = oldLines.length - suffixLen;
  const suffStartNew = newLines.length - suffixLen;
  for (let k = 0; k < suffixLen; k++) fullEdits.push({ type: 'equal', oldIdx: suffStart + k, newIdx: suffStartNew + k, text: oldLines[suffStart + k] });

  // Group into hunks with 3 lines of context
  return groupIntoHunks(fullEdits, 3);
}

function groupIntoHunks(edits, contextSize) {
  const hunks = [];
  let currentHunk = null;
  let oldLine = 1, newLine = 1;
  let lastChangeIdx = -999;

  for (let i = 0; i < edits.length; i++) {
    const edit = edits[i];
    const isChange = edit.type !== 'equal';

    if (isChange) {
      // Start new hunk if needed
      if (!currentHunk || i - lastChangeIdx > contextSize * 2) {
        // Close previous hunk with trailing context
        if (currentHunk) hunks.push(currentHunk);
        // Start new hunk: include leading context
        currentHunk = { oldStart: 0, newStart: 0, lines: [] };
        const ctxStart = Math.max(0, i - contextSize);
        // Recompute line numbers at ctxStart
        let oLine = 1, nLine = 1;
        for (let k = 0; k < ctxStart; k++) {
          if (edits[k].type === 'equal' || edits[k].type === 'remove') oLine++;
          if (edits[k].type === 'equal' || edits[k].type === 'add') nLine++;
        }
        currentHunk.oldStart = oLine;
        currentHunk.newStart = nLine;
        for (let k = ctxStart; k < i; k++) {
          currentHunk.lines.push({ type: 'context', text: edits[k].text });
        }
      }
      currentHunk.lines.push({ type: edit.type === 'add' ? 'add' : 'remove', text: edit.text });
      lastChangeIdx = i;
    } else if (currentHunk && i - lastChangeIdx <= contextSize) {
      currentHunk.lines.push({ type: 'context', text: edit.text });
    }
  }

  if (currentHunk) {
    // Compute counts
    hunks.push(currentHunk);
  }

  // Compute oldCount/newCount for each hunk
  for (const hunk of hunks) {
    hunk.oldCount = hunk.lines.filter(l => l.type === 'context' || l.type === 'remove').length;
    hunk.newCount = hunk.lines.filter(l => l.type === 'context' || l.type === 'add').length;
  }

  return hunks;
}

function renderDiffHunks(hunks) {
  if (!hunks.length) return '<div class="diff-loading">No differences</div>';

  let html = '';
  for (const hunk of hunks) {
    html += `<div class="diff-hunk-header">@@ -${hunk.oldStart},${hunk.oldCount} +${hunk.newStart},${hunk.newCount} @@</div>`;
    let oldNum = hunk.oldStart;
    let newNum = hunk.newStart;

    for (const line of hunk.lines) {
      let numA = '', numB = '', prefix = ' ', cls = 'context';
      if (line.type === 'remove') {
        numA = oldNum++;
        prefix = '-';
        cls = 'removed';
      } else if (line.type === 'add') {
        numB = newNum++;
        prefix = '+';
        cls = 'added';
      } else {
        numA = oldNum++;
        numB = newNum++;
      }
      html += `<div class="diff-line ${cls}">
        <span class="diff-line-number">${numA}</span>
        <span class="diff-line-number">${numB}</span>
        <span class="diff-line-content">${prefix}${escapeHtml(line.text)}</span>
      </div>`;
    }
  }
  return html;
}

// ══════════════════════════════════════════════════════
// VIEW: Search
// ══════════════════════════════════════════════════════
async function showSearch(query) {
  currentView = 'search';
  updateNav();

  if (!query) {
    setHtml('.content', `<div class="card"><h2>Search</h2><div class="empty">Enter a query in the search bar above.</div></div>`);
    return;
  }

  setHtml('.content', '<div class="loading">Searching...</div>');

  try {
    const results = await api(`/search?q=${encodeURIComponent(query)}`);
    const items = Array.isArray(results) ? results : [];

    if (!items.length) {
      setHtml('.content', `<div class="card"><h2>Search: "${escapeHtml(query)}"</h2><div class="empty">No results found.</div></div>`);
      return;
    }

    setHtml('.content', `
      <div class="card">
        <h2>Search: "${escapeHtml(query)}" (${items.length} results)</h2>
        ${items.map(r => {
          const status = r.status || 'proposed';
          const lane = r.lane || '';
          const agent = r.agent || {};
          const agentLabel = agent.agent_type || '';
          const timeAgo = relativeTime(r.created_at);
          const link = r.from_state && r.to_state
            ? `#/diff/${r.from_state}/${r.to_state}`
            : `#/state/${r.to_state || ''}`;
          return `<div class="search-result" onclick="navigate('${link}')">
            <div class="search-result-header">
              <span class="badge ${status}">${status}</span>
              <span style="color:var(--text-muted);font-size:11px">${escapeHtml(lane)}</span>
            </div>
            <div class="search-result-prompt">${escapeHtml(r.prompt || r.intent_prompt || '')}</div>
            <div class="search-result-meta">
              ${agentLabel ? `<span class="agent-badge">${escapeHtml(agentLabel)}</span>` : ''}
              <span>${timeAgo}</span>
              ${r.tags && r.tags.length ? `<span>${r.tags.map(t => escapeHtml(t)).join(', ')}</span>` : ''}
            </div>
          </div>`;
        }).join('')}
      </div>`);
  } catch (e) {
    setHtml('.content', `<div class="empty">Search error: ${escapeHtml(e.message)}</div>`);
  }
}

// ══════════════════════════════════════════════════════
// VIEW: Workspaces
// ══════════════════════════════════════════════════════
async function showWorkspaces() {
  currentView = 'workspaces';
  updateNav();
  setHtml('.content', '<div class="loading">Loading workspaces...</div>');

  try {
    const workspaces = await api('/workspaces');
    const items = Array.isArray(workspaces) ? workspaces : [];

    // Sort active first
    items.sort((a, b) => {
      const order = { active: 0, idle: 1, stale: 2, disposed: 3 };
      return (order[a.status] || 9) - (order[b.status] || 9);
    });

    if (!items.length) {
      setHtml('.content', `<div class="card"><h2>Workspaces</h2><div class="empty">No workspaces found.</div></div>`);
      return;
    }

    setHtml('.content', `
      <div class="card"><h2>Workspaces (${items.length})</h2></div>
      <div class="workspace-grid">
        ${items.map(ws => `
          <div class="workspace-card">
            <div class="workspace-card-header">
              <span class="workspace-status-dot ${ws.status || 'idle'}"></span>
              <span class="workspace-card-name">${escapeHtml(ws.name)}</span>
            </div>
            <div class="workspace-card-detail">
              <div><span class="ws-label">Lane:</span><span class="ws-lane-link" onclick="navigate('#/lane/${encodeURIComponent(ws.lane || '')}')">${escapeHtml(ws.lane || 'none')}</span></div>
              <div><span class="ws-label">Status:</span><span class="ws-value">${escapeHtml(ws.status || 'unknown')}</span></div>
              ${ws.agent_id ? `<div><span class="ws-label">Agent:</span><span class="ws-value">${escapeHtml(ws.agent_id)}</span></div>` : ''}
              ${ws.base_state ? `<div><span class="ws-label">Base:</span><span class="hash" onclick="navigate('#/state/${escapeHtml(ws.base_state)}')">${escapeHtml(safeSubstring(ws.base_state, 12))}</span></div>` : ''}
              ${ws.updated_at ? `<div><span class="ws-label">Updated:</span><span class="time-relative">${relativeTime(ws.updated_at)}</span></div>` : ''}
            </div>
          </div>
        `).join('')}
      </div>`);
  } catch (e) {
    setHtml('.content', `<div class="empty">Error: ${escapeHtml(e.message)}</div>`);
  }
}

// ══════════════════════════════════════════════════════
// VIEW: Trace / Lineage
// ══════════════════════════════════════════════════════
async function showTrace(stateId) {
  currentView = 'trace';
  updateNav();
  setHtml('.content', '<div class="loading">Loading lineage...</div>');

  try {
    const [traceData, laneData] = await Promise.all([api(`/trace?state=${stateId}`), api('/lanes')]);
    const chain = Array.isArray(traceData) ? traceData : [];

    if (!chain.length) {
      setHtml('.content', `<div class="card"><h2>Lineage for ${escapeHtml(safeSubstring(stateId, 12))}</h2><div class="empty">No lineage found.</div></div>`);
      return;
    }

    // Build fork point map: state_id -> [lane names that forked from it]
    const forkMap = {};
    for (const lane of laneData) {
      if (lane.fork_base) {
        if (!forkMap[lane.fork_base]) forkMap[lane.fork_base] = [];
        forkMap[lane.fork_base].push(lane.name);
      }
    }

    // SVG dimensions
    const nodeW = 320, nodeH = 56, gapY = 24, padX = 40, padY = 30;
    const svgW = nodeW + padX * 2;
    const svgH = chain.length * (nodeH + gapY) + padY * 2;

    let svg = `<svg class="trace-svg" width="${svgW}" height="${svgH}" xmlns="http://www.w3.org/2000/svg">`;

    // Draw links first (behind nodes)
    for (let i = 0; i < chain.length - 1; i++) {
      const y1 = padY + i * (nodeH + gapY) + nodeH;
      const y2 = padY + (i + 1) * (nodeH + gapY);
      const cx = padX + nodeW / 2;
      svg += `<line x1="${cx}" y1="${y1}" x2="${cx}" y2="${y2}" class="trace-link-line"/>`;
    }

    // Draw nodes
    for (let i = 0; i < chain.length; i++) {
      const t = chain[i];
      const x = padX;
      const y = padY + i * (nodeH + gapY);
      const stId = t.to_state || '';
      const isFork = forkMap[stId];
      const forkClass = isFork ? ' fork-point' : '';

      svg += `<g class="trace-node" onclick="navigate('#/state/${escapeHtml(stId)}')"
                 onmouseenter="showTraceTooltip(event, ${i})" onmouseleave="hideTooltip()">`;
      svg += `<rect x="${x}" y="${y}" width="${nodeW}" height="${nodeH}" class="trace-node-rect${forkClass}"/>`;
      svg += `<text x="${x + 10}" y="${y + 20}" class="trace-hash-text">${escapeHtml(safeSubstring(stId, 12))}</text>`;
      svg += `<text x="${x + 10}" y="${y + 36}" class="trace-prompt-text">${escapeHtml(safeSubstring(t.intent_prompt, 40))}</text>`;
      svg += `<text x="${x + nodeW - 10}" y="${y + 20}" class="trace-time-text" text-anchor="end">${relativeTime(t.created_at)}</text>`;
      if (isFork) {
        svg += `<text x="${x + nodeW - 10}" y="${y + 48}" class="trace-fork-label" text-anchor="end">fork: ${escapeHtml(forkMap[stId].join(', '))}</text>`;
      }
      svg += `</g>`;
    }

    svg += `</svg>`;

    // Store chain data for tooltips
    window._traceChain = chain;

    setHtml('.content', `
      <div class="card">
        <h2>Lineage: ${escapeHtml(safeSubstring(stateId, 12))}</h2>
        <div style="font-size:12px;color:var(--text-muted);margin-bottom:8px">${chain.length} transitions in chain</div>
      </div>
      <div class="trace-container">${svg}</div>`);
  } catch (e) {
    setHtml('.content', `<div class="empty">Error: ${escapeHtml(e.message)}</div>`);
  }
}

function showTraceTooltip(event, idx) {
  const t = window._traceChain && window._traceChain[idx];
  if (!t) return;
  const agent = t.agent || {};
  const content = `
    <div class="tt-label">Prompt</div>
    <div class="tt-value">${escapeHtml(t.intent_prompt || '')}</div>
    <div class="tt-label">Agent</div>
    <div class="tt-value">${escapeHtml(agent.agent_type || '')} ${agent.model ? '· ' + escapeHtml(agent.model) : ''}</div>
    <div class="tt-label">Status</div>
    <div class="tt-value">${escapeHtml(t.status || '')}</div>
    <div class="tt-label">Time</div>
    <div class="tt-value">${relativeTime(t.created_at)}</div>`;
  showTooltip(event.clientX, event.clientY, content);
}

// ══════════════════════════════════════════════════════
// VIEW: Timeline (flagship)
// ══════════════════════════════════════════════════════
let timelineData = null;  // cached for filter updates

async function showTimeline() {
  currentView = 'timeline';
  updateNav();
  setHtml('.content', '<div class="loading">Loading timeline...</div>');

  try {
    const laneData = await api('/lanes');
    const allHistories = await Promise.all(
      laneData.map(l => api(`/history?lane=${encodeURIComponent(l.name)}&limit=100`).catch(() => []))
    );

    // Build per-lane data
    const lanesWithHistory = [];
    for (let i = 0; i < laneData.length; i++) {
      const transitions = allHistories[i] || [];
      if (transitions.length > 0) {
        lanesWithHistory.push({ lane: laneData[i], transitions });
      }
    }

    // Also include lanes with 0 transitions (at the bottom)
    for (let i = 0; i < laneData.length; i++) {
      const transitions = allHistories[i] || [];
      if (transitions.length === 0) {
        lanesWithHistory.push({ lane: laneData[i], transitions: [] });
      }
    }

    timelineData = lanesWithHistory;

    renderTimeline('all', { accepted: true, rejected: true, proposed: true, evaluating: true });
  } catch (e) {
    setHtml('.content', `<div class="empty">Error: ${escapeHtml(e.message)}</div>`);
  }
}

function renderTimeline(timeRange, statusFilter) {
  const data = timelineData;
  if (!data) return;

  // Collect all transitions with time filter
  let allTransitions = [];
  const now = Date.now() / 1000;
  const rangeMap = { '1h': 3600, '6h': 21600, '24h': 86400, '7d': 604800, 'all': Infinity };
  const maxAge = rangeMap[timeRange] || Infinity;

  for (const d of data) {
    for (const t of d.transitions) {
      if (t.created_at && (now - t.created_at) <= maxAge) {
        allTransitions.push({ ...t, _laneName: d.lane.name });
      }
    }
  }

  if (!allTransitions.length) {
    setHtml('.content', `
      <div class="timeline-wrapper">
        ${renderTimelineFilters(timeRange, statusFilter)}
        <div class="empty" style="margin-top:40px">No transitions in this time range.</div>
      </div>`);
    return;
  }

  // Compute time bounds
  const timestamps = allTransitions.map(t => t.created_at);
  let minT = Math.min(...timestamps);
  let maxT = Math.max(...timestamps);
  if (maxT - minT < 60) { minT -= 30; maxT += 30; }  // At least 1 min range

  // Layout
  const laneNames = data.map(d => d.lane.name);
  const laneHeight = 50;
  const padTop = 30, padBot = 20, padLeft = 20, padRight = 40;
  const minSvgW = 900;
  const timeW = Math.max(minSvgW, (maxT - minT) / 60 * 15);  // ~15px per minute
  const svgW = timeW + padLeft + padRight;
  const svgH = laneNames.length * laneHeight + padTop + padBot;

  // Status colors
  const statusColors = { accepted: '#3fb950', rejected: '#f85149', proposed: '#d29922', evaluating: '#bc8cff', superseded: '#8b949e' };

  // Build SVG
  let svg = `<svg class="timeline-svg" width="${svgW}" height="${svgH}" xmlns="http://www.w3.org/2000/svg">`;

  // Defs: arrowhead
  svg += `<defs><marker id="arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
    <polygon points="0 0, 8 3, 0 6" fill="#bc8cff" opacity="0.7"/></marker></defs>`;

  // Lane backgrounds and dividers
  for (let i = 0; i < laneNames.length; i++) {
    const y = padTop + i * laneHeight;
    if (i % 2 === 0) svg += `<rect x="0" y="${y}" width="${svgW}" height="${laneHeight}" fill="rgba(255,255,255,0.015)"/>`;
    if (i > 0) svg += `<line x1="0" y1="${y}" x2="${svgW}" y2="${y}" class="timeline-lane-divider"/>`;
  }

  // Time axis labels
  const numLabels = Math.min(12, Math.max(4, Math.floor(timeW / 100)));
  for (let i = 0; i <= numLabels; i++) {
    const frac = i / numLabels;
    const x = padLeft + frac * timeW;
    const t = minT + frac * (maxT - minT);
    svg += `<line x1="${x}" y1="${padTop}" x2="${x}" y2="${svgH - padBot}" class="timeline-time-line"/>`;
    svg += `<text x="${x}" y="${padTop - 8}" class="timeline-time-label" text-anchor="middle">${relativeTime(t)}</text>`;
  }

  // Promote arrows (draw before nodes so they appear behind)
  const promoteTransitions = allTransitions.filter(t => t.tags && t.tags.includes('promote'));
  for (const pt of promoteTransitions) {
    const fromLaneTag = (pt.tags || []).find(tag => tag.startsWith('from:'));
    if (!fromLaneTag) continue;
    const fromLaneName = fromLaneTag.substring(5);
    const fromLaneIdx = laneNames.indexOf(fromLaneName);
    const toLaneIdx = laneNames.indexOf(pt._laneName);
    if (fromLaneIdx === -1 || toLaneIdx === -1) continue;

    const x = padLeft + ((pt.created_at - minT) / (maxT - minT)) * timeW;
    const y1 = padTop + fromLaneIdx * laneHeight + laneHeight / 2;
    const y2 = padTop + toLaneIdx * laneHeight + laneHeight / 2;
    svg += `<line x1="${x}" y1="${y1}" x2="${x}" y2="${y2}" class="timeline-promote-arrow"/>`;
  }

  // Transition nodes
  window._timelineTransitions = allTransitions;
  for (let i = 0; i < allTransitions.length; i++) {
    const t = allTransitions[i];
    if (!statusFilter[t.status]) continue;
    const laneIdx = laneNames.indexOf(t._laneName);
    if (laneIdx === -1) continue;

    const x = padLeft + ((t.created_at - minT) / (maxT - minT)) * timeW;
    const y = padTop + laneIdx * laneHeight + laneHeight / 2;
    const color = statusColors[t.status] || statusColors.proposed;
    const r = (t.tags && t.tags.includes('promote')) ? 7 : 5;

    const diffTarget = t.from_state && t.to_state ? `#/diff/${t.from_state}/${t.to_state}` : `#/state/${t.to_state || ''}`;
    svg += `<circle class="timeline-node" cx="${x}" cy="${y}" r="${r}" fill="${color}" stroke="${color}" stroke-width="1"
      onclick="navigate('${diffTarget}')"
      onmouseenter="showTimelineTooltip(event, ${i})" onmouseleave="hideTooltip()"/>`;
  }

  svg += `</svg>`;

  // Render lane labels
  const labels = laneNames.map(name =>
    `<div class="timeline-lane-label" style="height:${laneHeight}px">${escapeHtml(name)}</div>`
  ).join('');

  setHtml('.content', `
    <div class="timeline-wrapper">
      ${renderTimelineFilters(timeRange, statusFilter)}
      <div class="timeline-scroll-area">
        <div class="timeline-lane-labels" style="padding-top:${padTop}px">${labels}</div>
        <div class="timeline-svg-container">${svg}</div>
      </div>
    </div>`);

  // Wire up filter controls
  wireTimelineFilters();
}

function renderTimelineFilters(timeRange, statusFilter) {
  const ranges = ['1h', '6h', '24h', '7d', 'all'];
  const statuses = ['accepted', 'rejected', 'proposed', 'evaluating'];

  return `
    <div class="timeline-filters">
      <div class="timeline-filter-group">
        <label>Time:</label>
        <select id="tl-time-range">
          ${ranges.map(r => `<option value="${r}" ${r === timeRange ? 'selected' : ''}>${r === 'all' ? 'All time' : 'Last ' + r}</option>`).join('')}
        </select>
      </div>
      <div class="timeline-filter-group">
        <label>Status:</label>
        ${statuses.map(s => `
          <input type="checkbox" id="tl-status-${s}" ${statusFilter[s] ? 'checked' : ''}>
          <label for="tl-status-${s}" class="filter-cb-label">${s}</label>
        `).join('')}
      </div>
    </div>`;
}

function wireTimelineFilters() {
  const timeSelect = $('#tl-time-range');
  if (!timeSelect) return;

  const applyFilters = () => {
    const range = timeSelect.value;
    const sf = {};
    for (const s of ['accepted', 'rejected', 'proposed', 'evaluating']) {
      const cb = $(`#tl-status-${s}`);
      sf[s] = cb ? cb.checked : true;
    }
    renderTimeline(range, sf);
  };

  timeSelect.addEventListener('change', applyFilters);
  for (const s of ['accepted', 'rejected', 'proposed', 'evaluating']) {
    const cb = $(`#tl-status-${s}`);
    if (cb) cb.addEventListener('change', applyFilters);
  }
}

function showTimelineTooltip(event, idx) {
  const t = window._timelineTransitions && window._timelineTransitions[idx];
  if (!t) return;
  const agent = t.agent || {};
  const cost = t.cost || {};
  const tokensIn = cost.tokens_in || 0;
  const tokensOut = cost.tokens_out || 0;
  const content = `
    <div class="tt-label">Prompt</div>
    <div class="tt-value">${escapeHtml(safeSubstring(t.intent_prompt, 80))}</div>
    <div class="tt-label">Agent</div>
    <div class="tt-value">${escapeHtml(agent.agent_type || '')} ${agent.model ? '· ' + escapeHtml(agent.model) : ''}</div>
    <div class="tt-label">Status</div>
    <div class="tt-value"><span class="badge ${t.status}">${t.status}</span></div>
    <div class="tt-label">Tokens</div>
    <div class="tt-value tt-mono">${formatTokens(tokensIn)} in / ${formatTokens(tokensOut)} out</div>
    <div class="tt-label">Lane</div>
    <div class="tt-value">${escapeHtml(t._laneName || t.lane || '')}</div>
    <div class="tt-label">Time</div>
    <div class="tt-value">${relativeTime(t.created_at)}</div>
    ${t.tags && t.tags.length ? `<div class="tt-label">Tags</div><div class="tt-value">${t.tags.map(tag => escapeHtml(tag)).join(', ')}</div>` : ''}`;
  showTooltip(event.clientX, event.clientY, content);
}

// ══════════════════════════════════════════════════════
// Init
// ══════════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', () => {
  // Hash routing
  window.addEventListener('hashchange', resolveRoute);

  // Search form
  const searchForm = $('#search-form');
  if (searchForm) {
    searchForm.addEventListener('submit', (e) => {
      e.preventDefault();
      const q = $('#search-input').value.trim();
      if (q) navigate(`#/search?q=${encodeURIComponent(q)}`);
    });
  }

  // Initial route
  if (!window.location.hash || window.location.hash === '#/') {
    window.location.hash = '#/dashboard';
  }
  resolveRoute();

  // Auto-refresh for dashboard and workspaces
  setInterval(() => {
    if (currentView === 'dashboard') showDashboard();
    else if (currentView === 'workspaces') showWorkspaces();
  }, 5000);
});
