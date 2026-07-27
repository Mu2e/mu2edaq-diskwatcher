/* Shared dashboard helpers.
 *
 * Each dashboard registers its tables with `registerTable()`, supplies a
 * `renderRows` function, and calls `startDashboard()`.  Sorting, the refresh
 * controls, the stat cards and the common cell builders live here so the three
 * pages do not re-implement them.
 *
 * The server ships a numeric `*_rank` for every state, so this file never has
 * to encode which state is more severe than which.
 */

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

const DASH = '—';

// ---- sortable tables -------------------------------------------------
// name -> {accessors: {col: fn}, state: {col, dir}, render: fn}
const TABLES = {};

function registerTable(name, accessors, defaultCol, render) {
  TABLES[name] = {
    accessors: accessors,
    state: {col: defaultCol, dir: 1},
    render: render,
  };
}

const SORT_ICON = {
  none: '<i class="bi bi-arrow-down-up text-muted ms-1 small"></i>',
  asc:  '<i class="bi bi-arrow-up ms-1 small"></i>',
  desc: '<i class="bi bi-arrow-down ms-1 small"></i>',
};

function updateSortIndicators(name) {
  const table = TABLES[name];
  for (const col of Object.keys(table.accessors)) {
    const el = document.getElementById('sort-' + name + '-' + col);
    if (el) el.innerHTML = (table.state.col === col)
      ? (table.state.dir > 0 ? SORT_ICON.asc : SORT_ICON.desc)
      : SORT_ICON.none;
  }
}

function sortRows(name, rows) {
  const table = TABLES[name];
  const {col, dir} = table.state;
  const accessor = table.accessors[col];
  const byName = table.accessors.name;
  return [...rows].sort((a, b) => {
    const av = accessor(a), bv = accessor(b);
    if (typeof av === 'string' || typeof bv === 'string') {
      return dir * String(av || '').localeCompare(String(bv || ''));
    }
    // Entries with no value sort to the end whichever way the column points,
    // so a missing file never displaces a real reading at the top.
    const sentinel = dir > 0 ? Infinity : -Infinity;
    const an = (av === null || av === undefined) ? sentinel : av;
    const bn = (bv === null || bv === undefined) ? sentinel : bv;
    if (an !== bn) return dir * (an - bn);
    return String(byName ? byName(a) : '').localeCompare(String(byName ? byName(b) : ''));
  });
}

function toggleSort(name, col) {
  const table = TABLES[name];
  table.state.dir = (table.state.col === col) ? -table.state.dir : 1;
  table.state.col = col;
  if (currentData) renderAll(currentData);
}

// ---- cell builders shared by the dashboards --------------------------
function badge(state) {
  const key = String(state || 'unknown').toLowerCase();
  return '<span class="badge badge-' + key + '">' + escHtml(key) + '</span>';
}

function nameCell(entry) {
  const sshBadge = entry.remote
    ? ' <span class="badge bg-secondary ms-1" title="Remote via SSH: ' +
      escHtml(entry.ssh_host) + '"><i class="bi bi-hdd-network"></i> ' +
      escHtml(entry.ssh_host) + '</span>'
    : '';
  if (entry.label && entry.label !== entry.path) {
    return '<td><strong>' + escHtml(entry.label) + '</strong>' + sshBadge + '<br>' +
           '<span class="path-cell text-muted">' + escHtml(entry.path) + '</span></td>';
  }
  return '<td><span class="path-cell">' + escHtml(entry.path) + '</span>' +
         sshBadge + '</td>';
}

// A complete <td> holding a progress bar, with an optional caption underneath.
// The bar width is clamped to 0..100 but the printed figure is not, so a file
// that has blown past its limit still reads e.g. "220.4%".
function progressBar(pct, cls, title, caption) {
  if (pct === null || pct === undefined) {
    return '<td class="text-muted small">' + DASH + '</td>';
  }
  const width = Math.max(0, Math.min(100, pct)).toFixed(1);
  return '<td style="min-width:170px">' +
    '<div class="progress mb-1" style="height:16px" title="' + escHtml(title || '') + '">' +
    '<div class="progress-bar ' + cls + '" role="progressbar" style="width:' + width + '%" ' +
    'aria-valuenow="' + width + '" aria-valuemin="0" aria-valuemax="100">' +
    pct.toFixed(1) + '%</div></div>' +
    (caption ? '<small class="text-muted">' + caption + '</small>' : '') +
    '</td>';
}

const BAR_CLASS = {
  GOOD: 'bg-success', WARNING: 'bg-warning', CRITICAL: 'bg-danger',
  FULL: 'bg-danger', EMPTY: 'bg-secondary', MISSING: 'bg-secondary',
  UNKNOWN: 'bg-secondary',
};

// Render `warning / critical / full` from the raw YAML strings, with the
// resolved byte values as a tooltip and a marker when they do not escalate.
function limitsCell(limitsStr, limitsBytes, ordered) {
  if (!limitsStr) return '<td class="text-muted small">' + DASH + '</td>';
  let html = '<td class="limit-cell">';
  for (const level of ['warning', 'critical', 'full']) {
    const raw = limitsStr[level];
    if (!raw) continue;
    const resolved = limitsBytes ? limitsBytes[level] : null;
    const title = (resolved !== null && resolved !== undefined)
      ? level + ' = ' + resolved.toLocaleString() + ' bytes' : level;
    html += '<div title="' + escHtml(title) + '"><span class="lvl">' + level +
            '</span> ' + escHtml(raw) + '</div>';
  }
  if (ordered === false) {
    html += '<div class="text-danger" title="Thresholds do not escalate in the ' +
            'expected order; some alarm states are unreachable">' +
            '<i class="bi bi-exclamation-triangle-fill"></i> misordered</div>';
  }
  return html + '</td>';
}

// ---- stat cards ------------------------------------------------------
function renderStatCards(prefix, counts) {
  for (const [key, value] of Object.entries(counts || {})) {
    const el = document.getElementById('stat-' + prefix + '-' + key);
    if (el) el.textContent = value;
  }
}

// ---- data fetch + refresh loop ---------------------------------------
let currentData = null;
let renderAll = function () {};
let refreshTimer = null;

const REFRESH_KEY = 'diskwatcher.refreshMs';

function applyRefreshInterval(ms) {
  if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
  const label = document.getElementById('refresh-label');
  if (ms > 0) {
    refreshTimer = setInterval(refreshData, ms);
    if (label) label.textContent = 'Auto-refreshes every ' +
      (ms >= 60000 ? (ms / 60000) + ' min' : (ms / 1000) + ' s');
  } else if (label) {
    label.textContent = 'Auto-refresh off';
  }
}

let dataUrl = '/api/status';

function refreshData() {
  fetch(dataUrl, {cache: 'no-store'})
    .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
    .then(data => {
      currentData = data;
      renderAll(data);
      const stamp = document.getElementById('last-update');
      if (stamp) stamp.textContent = '✓ Updated ' + new Date().toLocaleTimeString();
    })
    .catch(err => {
      console.error('Failed to fetch ' + dataUrl + ':', err);
      const stamp = document.getElementById('last-update');
      if (stamp) stamp.textContent = '⚠ Fetch failed';
    });
}

// Fill a <tbody>, or show a placeholder row when there is nothing to show.
function fillTable(id, rowsHtml, colspan, emptyMessage) {
  const tbody = document.getElementById(id);
  if (!tbody) return;
  tbody.innerHTML = rowsHtml ||
    '<tr><td colspan="' + colspan + '" class="text-muted p-3">' +
    escHtml(emptyMessage) + '</td></tr>';
}

function startDashboard(url, render) {
  dataUrl = url;
  renderAll = render;

  const select = document.getElementById('refresh-interval');
  const stored = parseInt(window.localStorage.getItem(REFRESH_KEY), 10);
  const initial = Number.isFinite(stored) ? stored : 5000;
  if (select) {
    select.value = String(initial);
    select.addEventListener('change', function () {
      const ms = parseInt(this.value, 10);
      window.localStorage.setItem(REFRESH_KEY, String(ms));
      applyRefreshInterval(ms);
    });
  }
  const button = document.getElementById('refresh-now');
  if (button) button.addEventListener('click', refreshData);

  Object.keys(TABLES).forEach(updateSortIndicators);
  refreshData();
  applyRefreshInterval(initial);
}
