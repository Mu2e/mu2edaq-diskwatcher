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

// ---- grouping by host -------------------------------------------------
// The <tbody> is rebuilt from scratch on every poll -- every 5 s by default --
// so a collapsed group cannot be represented by a class on the DOM node: the
// next refresh would spring it back open.  Collapse state is held here and
// re-applied at render time, and persisted so it also survives a page reload
// and navigation between the two dashboards.
// A group's open/closed state has two sources, in order of precedence:
//
//   1. an explicit click by the operator, remembered per host per page
//   2. otherwise the group's own health -- open as soon as one entry reaches
//      CRITICAL or worse, collapsed otherwise, so what needs attention is on
//      screen without being asked for
//
// Storage therefore holds *overrides* (id -> true/false), not a list of
// collapsed hosts: absent means "no opinion, follow the health".
const GROUP_KEY = 'diskwatcher.hostGroups';
const LOCAL_HOST = 'local';

// Fallback only: every real payload from api_space()/api_sizes() carries
// alert_rank, so the CRITICAL threshold is defined once, server-side, and the
// client never hardcodes a state name or its position in the ordering. This
// value is used only if a payload is somehow missing the field.
const DEFAULT_ALERT_RANK = 3;

function loadOverrides() {
  try {
    const raw = window.localStorage.getItem(GROUP_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    // Anything but a plain object (an older format, a hand-edited value) is
    // discarded rather than half-honoured.
    return (parsed && typeof parsed === 'object' && !Array.isArray(parsed))
      ? parsed : {};
  } catch (err) {
    // Corrupt JSON or storage disabled must not take the dashboard down.
    return {};
  }
}

let hostOverrides = loadOverrides();

function saveOverrides() {
  try {
    window.localStorage.setItem(GROUP_KEY, JSON.stringify(hostOverrides));
  } catch (err) {
    // Private browsing: toggling still works, it just will not persist.
  }
}

function hostOf(entry) {
  if (!entry.remote) return LOCAL_HOST;
  return entry.ssh_host || 'unknown host';
}

// Keyed per table, so collapsing a host on Disk Space does not silently hide
// that host's files on File Sizes -- the two pages answer different questions.
// The separator is written as an escape on purpose: a raw control byte in
// source is easily mangled by an editor, a diff or a copy-paste.
function collapseId(table, host) { return table + '\u001f' + host; }

// True once any entry's rank reaches alertRank (CRITICAL) or worse.  Compares
// against the numeric rank the server ships, never a state name, so this stays
// correct however many states sit above CRITICAL (today: FULL, UNKNOWN,
// MISSING) without being told about any of them.  A missing rank is treated
// as needing attention too: an entry whose health could not be established
// should hold its group open rather than let it hide.
function needsAttention(entries, rankKey, alertRank) {
  return entries.some(e => {
    const rank = e[rankKey];
    return rank === null || rank === undefined || rank >= alertRank;
  });
}

function isCollapsed(table, host, entries, rankKey, alertRank) {
  const override = hostOverrides[collapseId(table, host)];
  if (override === true || override === false) return override;
  return !needsAttention(entries, rankKey, alertRank);
}

// `collapsedNow` is the state the header was rendered in, passed back by the
// click so the toggle flips what the operator actually sees.  The handler has
// no entries of its own to re-derive the health from.
function toggleGroup(table, host, collapsedNow) {
  hostOverrides[collapseId(table, host)] = !collapsedNow;
  saveOverrides();
  if (currentData) renderAll(currentData);
}

// Group already-sorted rows by host, preserving the sort order within each
// group.  Local paths lead, then remote hosts alphabetically: a host keeping a
// fixed position is worth more than floating the worst one to the top, because
// the header carries the group's alarm states even while collapsed.
function groupByHost(rows) {
  const groups = new Map();
  for (const r of rows) {
    const host = hostOf(r);
    if (!groups.has(host)) groups.set(host, []);
    groups.get(host).push(r);
  }
  const out = [];
  groups.forEach((entries, host) => out.push({host: host, entries: entries}));
  return out.sort((a, b) => {
    if (a.host === b.host) return 0;
    if (a.host === LOCAL_HOST) return -1;
    if (b.host === LOCAL_HOST) return 1;
    return a.host.localeCompare(b.host);
  });
}

// One badge per state present in the group, worst first, so a collapsed host
// still reports what is wrong inside it.
function groupChips(entries, stateKey, rankKey) {
  const seen = {};
  for (const e of entries) {
    const state = String(e[stateKey] || 'UNKNOWN');
    if (!seen[state]) {
      const rank = e[rankKey];
      seen[state] = {n: 0, rank: (rank === null || rank === undefined) ? -1 : rank};
    }
    seen[state].n += 1;
  }
  return Object.keys(seen)
    .sort((a, b) => seen[b].rank - seen[a].rank)
    .map(state => badge(state) +
      (seen[state].n > 1
        ? '<small class="text-muted ms-1">&times;' + seen[state].n + '</small>'
        : ''))
    .join(' ');
}

function groupHeaderRow(table, host, entries, collapsed, opts) {
  const isLocal = host === LOCAL_HOST;
  const label = isLocal ? 'local' : host;
  const why = collapsed
    ? 'Collapsed: nothing here needs attention. Click to open.'
    : 'Click to collapse.';
  const where = isLocal
    ? 'Paths on the host running diskwatcher'
    : 'Remote paths read over SSH from ' + host;

  return '<tr class="group-row">' +
    '<td colspan="' + opts.colspan + '">' +
    '<button type="button" class="group-toggle" ' +
      'title="' + escHtml(where + '. ' + why) + '" ' +
      'aria-expanded="' + (collapsed ? 'false' : 'true') + '" ' +
      'onclick="toggleGroup(\'' + escHtml(table) + '\',\'' + escHtml(host) +
        '\',' + (collapsed ? 'true' : 'false') + ')">' +
      '<i class="bi bi-caret-' + (collapsed ? 'right' : 'down') + '-fill"></i>' +
      '<i class="bi bi-' + (isLocal ? 'hdd' : 'hdd-network') + '"></i>' +
      '<span class="group-host">' + escHtml(label) + '</span>' +
      '<span class="group-count">' + entries.length +
        (entries.length === 1 ? ' entry' : ' entries') + '</span>' +
    '</button>' +
    '<span class="group-chips">' +
      groupChips(entries, opts.stateKey, opts.rankKey) + '</span>' +
    '</td></tr>';
}

// ---- peer groups -------------------------------------------------------
// A peer is another diskwatcher instance whose /api/state this one fetches.
// Its entries arrive already evaluated -- the peer owns its thresholds -- and
// are shown as one group per peer after the local host groups, headed by the
// peer's connection details.  The collapse memory is the same store the host
// groups use, keyed by URL so a relabelled peer keeps its setting.
const PEER_KEY_PREFIX = 'peer:';

function peerKey(peer) { return PEER_KEY_PREFIX + peer.url; }

// A peer that is not delivering current data always holds its group open:
// "unreachable" is the thing on this page most worth an operator's eye, and
// it has no entry rank of its own to say so.  Connected peers follow their
// entries' health exactly like a host group.
function peerNeedsAttention(peer, entries, rankKey, alertRank) {
  if (peer.status !== 'ok') return peer.status !== 'disabled';
  return needsAttention(entries, rankKey, alertRank);
}

function isPeerCollapsed(table, peer, entries, rankKey, alertRank) {
  const override = hostOverrides[collapseId(table, peerKey(peer))];
  if (override === true || override === false) return override;
  return !peerNeedsAttention(peer, entries, rankKey, alertRank);
}

// Stable order: by label, then URL.  Local groups always come first, so a
// peer's position never depends on how it is doing.
function sortPeers(peers) {
  return [...peers].sort((a, b) =>
    String(a.label).localeCompare(String(b.label)) ||
    String(a.url).localeCompare(String(b.url)));
}

// Short human age: "12 s", "3 min", "2 h".
function fmtAge(seconds) {
  if (seconds === null || seconds === undefined) return DASH;
  if (seconds < 90) return Math.round(seconds) + ' s';
  if (seconds < 5400) return Math.round(seconds / 60) + ' min';
  return (seconds / 3600).toFixed(1) + ' h';
}

const PEER_BADGE = {
  ok:       ['bg-success',   'connected'],
  error:    ['bg-danger',    'unreachable'],
  pending:  ['bg-secondary', 'connecting'],
  disabled: ['bg-secondary', 'disabled'],
};

function peerStatusBadge(peer) {
  const [cls, text] = PEER_BADGE[peer.status] || PEER_BADGE.pending;
  const title = peer.status === 'error'
    ? (peer.error || 'fetch failed') +
      (peer.last_ok_str ? '. Last good data ' + peer.last_ok_str : '')
    : text;
  return '<span class="badge ' + cls + ' peer-status" title="' + escHtml(title) +
         '">' + text + '</span>';
}

// One line of connection facts after the label: the URL as a link to the same
// page on the peer, the hostname it reports, its version, and how old the
// data is (time since we fetched it plus the peer's own poll age at the time).
function peerDetails(peer) {
  const parts = [];
  parts.push('<a class="group-url" href="' + escHtml(peer.url + window.location.pathname) +
             '" target="_blank" rel="noopener" title="Open this page on the peer">' +
             escHtml(peer.url) + ' <i class="bi bi-box-arrow-up-right"></i></a>');
  if (peer.hostname && peer.hostname !== peer.label) parts.push(escHtml(peer.hostname));
  if (peer.version) parts.push('v' + escHtml(peer.version));
  if (peer.source === 'discovered') {
    parts.push('<span class="badge bg-info text-dark peer-source" ' +
               'title="Found by mu2edaq-discovery, not listed in the config">discovered</span>' +
               (peer.discovery_missing
                 ? ' <span class="text-warning-emphasis">not seen by discovery for ' +
                   fmtAge(peer.discovery_seen_age_s) + '</span>'
                 : ''));
  }
  if (peer.status === 'ok') {
    const age = (peer.fetched_age_s || 0) + (peer.peer_poll_age_s || 0);
    parts.push('data ' + fmtAge(age) + ' old');
  } else if (peer.status === 'error') {
    parts.push('<span class="text-danger">' + escHtml(peer.error || 'fetch failed') + '</span>');
    if (peer.last_ok_age_s !== null && peer.last_ok_age_s !== undefined) {
      parts.push('last data ' + fmtAge(peer.last_ok_age_s) + ' ago');
    }
  }
  return parts.join('<span class="group-sep">&middot;</span>');
}

function peerHeaderRow(table, peer, entries, collapsed, opts) {
  const why = collapsed ? 'Click to open.' : 'Click to collapse.';
  return '<tr class="group-row peer-row peer-' + escHtml(peer.status) + '">' +
    '<td colspan="' + opts.colspan + '">' +
    '<button type="button" class="group-toggle" ' +
      'title="' + escHtml('Peer diskwatcher at ' + peer.url + '. ' + why) + '" ' +
      'aria-expanded="' + (collapsed ? 'false' : 'true') + '" ' +
      'onclick="toggleGroup(\'' + escHtml(table) + '\',\'' + escHtml(peerKey(peer)) +
        '\',' + (collapsed ? 'true' : 'false') + ')">' +
      '<i class="bi bi-caret-' + (collapsed ? 'right' : 'down') + '-fill"></i>' +
      '<i class="bi bi-diagram-3"></i>' +
      '<span class="group-host">' + escHtml(peer.label) + '</span>' +
      '<span class="group-count">' + entries.length +
        (entries.length === 1 ? ' entry' : ' entries') + '</span>' +
    '</button>' +
    peerStatusBadge(peer) +
    '<span class="group-details">' + peerDetails(peer) + '</span>' +
    '<span class="group-chips">' +
      groupChips(entries, opts.stateKey, opts.rankKey) + '</span>' +
    '</td></tr>';
}

// What an open peer group shows when it has no rows to show.
function peerPlaceholderRow(peer, colspan) {
  let text;
  if (peer.status === 'pending')       text = 'Connecting…';
  else if (peer.status === 'disabled') text = 'Disabled in the configuration.';
  else if (peer.status === 'error')    text = 'Unreachable, and no data has been received yet.';
  else                                 text = 'Nothing on this peer applies to this page.';
  return '<tr class="peer-placeholder"><td colspan="' + colspan +
         '" class="text-muted small ps-4">' + escHtml(text) + '</td></tr>';
}

// Build a whole <tbody>: a header row per host, followed by that host's rows
// unless it is collapsed; then one group per peer.  `rowsFor` is the page's
// own row builder.  `opts.peers` is the `peers` array from a `?peers=1`
// payload and may be absent.
function groupedRows(table, rows, rowsFor, opts) {
  const peers = opts.peers || [];
  if (!rows.length && !peers.length) return null;
  const alertRank = (opts.alertRank === null || opts.alertRank === undefined)
    ? DEFAULT_ALERT_RANK : opts.alertRank;
  let html = '';
  for (const group of groupByHost(rows)) {
    const collapsed = isCollapsed(table, group.host, group.entries, opts.rankKey, alertRank);
    html += groupHeaderRow(table, group.host, group.entries, collapsed, opts);
    if (!collapsed) html += rowsFor(group.entries);
  }
  for (const peer of sortPeers(peers)) {
    const entries = sortRows(table, peer.entries || []);
    const collapsed = isPeerCollapsed(table, peer, entries, opts.rankKey, alertRank);
    html += peerHeaderRow(table, peer, entries, collapsed, opts);
    if (collapsed) continue;
    if (!entries.length) {
      html += peerPlaceholderRow(peer, opts.colspan);
    } else if (peer.stale) {
      // Retained from the last good fetch: still worth seeing, but dimmed so
      // nobody reads a reading from before the outage as current.
      html += rowsFor(entries).replace(/<tr class="/g, '<tr class="peer-stale ');
    } else {
      html += rowsFor(entries);
    }
  }
  return html;
}

// The one-line roll-up under the stat cards: one chip per peer, linking to
// the same page there.  Hidden when no peers are configured, so a standalone
// instance looks exactly as it did.
function renderPeerStrip(peers) {
  const el = document.getElementById('peer-strip');
  if (!el) return;
  if (!peers || !peers.length) { el.hidden = true; el.innerHTML = ''; return; }
  let html = '<span class="text-muted me-2"><i class="bi bi-diagram-3"></i> Peers:</span>';
  for (const peer of sortPeers(peers)) {
    const [cls] = PEER_BADGE[peer.status] || PEER_BADGE.pending;
    const title = peer.url + (peer.status === 'error' ? ' — ' + (peer.error || 'unreachable') : '');
    html += '<a class="peer-chip ' + cls + '" href="' +
            escHtml(peer.url + window.location.pathname) + '" target="_blank" ' +
            'rel="noopener" title="' + escHtml(title) + '">' +
            escHtml(peer.label) + ' <small>' + (peer.entries ? peer.entries.length : 0) +
            '</small></a>';
  }
  el.innerHTML = html;
  el.hidden = false;
}

// "(5)" for a standalone instance; "(5 local + 12 from 2 peers)" with peers.
function countLabel(localCount, peers) {
  if (!peers || !peers.length) return '(' + localCount + ')';
  const fromPeers = peers.reduce((n, p) => n + (p.entries ? p.entries.length : 0), 0);
  return '(' + localCount + ' local + ' + fromPeers + ' from ' + peers.length +
         (peers.length === 1 ? ' peer)' : ' peers)');
}

// ---- expand / collapse every group ------------------------------------
// Both write the same per-group overrides a header click does, for every
// group present in the current payload (hosts from the entries, plus peers),
// so the result survives the next poll exactly like a manual toggle.
function groupIdsIn(table, data) {
  const ids = [];
  for (const g of groupByHost(data.entries || [])) ids.push(collapseId(table, g.host));
  for (const peer of (data.peers || [])) ids.push(collapseId(table, peerKey(peer)));
  return ids;
}

function setAllGroups(table, collapsed) {
  if (!currentData) return;
  for (const id of groupIdsIn(table, currentData)) hostOverrides[id] = collapsed;
  saveOverrides();
  renderAll(currentData);
}

// Drop every override for this table so groups follow their health again.
function resetGroups(table) {
  const prefix = table + '\u001f';
  for (const id of Object.keys(hostOverrides)) {
    if (id.startsWith(prefix)) delete hostOverrides[id];
  }
  saveOverrides();
  if (currentData) renderAll(currentData);
}

// ---- state filter ------------------------------------------------------
// Clicking a stat card (other than GOOD, which is the quiet majority) narrows
// the table to entries in that category; clicking it again, the TOTAL card or
// the Reset link clears it.  The cards keep showing the unfiltered counts.
// The choice is held per table and persisted like the group overrides.
// 'unavailable' is the one composite category: it is the card the pages show
// for MISSING and UNKNOWN together, so it matches both.
const FILTER_KEY = 'diskwatcher.stateFilter';
const FILTER_ALIASES = {unavailable: ['missing', 'unknown']};

function loadFilters() {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(FILTER_KEY) || '{}');
    return (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) ? parsed : {};
  } catch (err) {
    return {};
  }
}

let stateFilters = loadFilters();

function saveFilters() {
  try {
    window.localStorage.setItem(FILTER_KEY, JSON.stringify(stateFilters));
  } catch (err) {
    // Storage unavailable: the filter still applies until the page is closed.
  }
}

function activeFilter(table) {
  const key = stateFilters[table];
  return (typeof key === 'string' && key && key !== 'all') ? key : null;
}

function filterMatches(key, state) {
  const st = String(state || 'unknown').toLowerCase();
  const names = FILTER_ALIASES[key] || [key];
  return names.indexOf(st) !== -1;
}

// Toggle: the active filter clicked again clears it; 'all' or null clears.
function setStateFilter(table, key) {
  const next = (!key || key === 'all' || activeFilter(table) === key) ? null : key;
  if (next) stateFilters[table] = next; else delete stateFilters[table];
  saveFilters();
  if (currentData) renderAll(currentData);
}

function clearStateFilter(table) { setStateFilter(table, null); }

function applyStateFilter(table, entries, stateKey) {
  const key = activeFilter(table);
  if (!key) return entries;
  return entries.filter(e => filterMatches(key, e[stateKey]));
}

// Peers keep their identity but only their matching rows; a peer with nothing
// matching is left out entirely while a filter is active, so the table shows
// just the category asked for.
function filterPeers(table, peers, stateKey) {
  const key = activeFilter(table);
  if (!key) return peers;
  return peers
    .map(p => Object.assign({}, p, {entries: (p.entries || []).filter(e => filterMatches(key, e[stateKey]))}))
    .filter(p => p.entries.length);
}

// Highlight the active card and fill the "Showing N of M" line.
function renderFilterBar(prefix, table, shown, total) {
  const key = activeFilter(table);
  document.querySelectorAll('.stat-card[data-filter]').forEach(card => {
    if (!card.id.startsWith('card-' + prefix + '-')) return;
    card.classList.toggle('active-filter', card.dataset.filter === key);
    card.setAttribute('aria-pressed', card.dataset.filter === key ? 'true' : 'false');
  });
  const bar = document.getElementById('filter-bar-' + prefix);
  if (!bar) return;
  if (!key) { bar.hidden = true; bar.innerHTML = ''; return; }
  bar.innerHTML = '<i class="bi bi-funnel-fill"></i> Showing <strong>' + shown + '</strong> of ' +
    total + ' entries in state ' + badge(key) +
    ' <button type="button" class="btn btn-link btn-sm p-0 ms-2 align-baseline" ' +
    'onclick="clearStateFilter(\'' + escHtml(table) + '\')">Reset filter</button>';
  bar.hidden = false;
}

// Make every filterable card for `prefix` respond to click and keyboard.
function installStatFilters(prefix, table) {
  document.querySelectorAll('.stat-card[data-filter]').forEach(card => {
    if (!card.id.startsWith('card-' + prefix + '-')) return;
    const act = () => setStateFilter(table, card.dataset.filter);
    card.addEventListener('click', act);
    card.addEventListener('keydown', ev => {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); act(); }
    });
  });
}

// Entries across the local list and every peer, for the "of M" figure.
function totalEntries(entries, peers) {
  return (entries || []).length +
    (peers || []).reduce((n, p) => n + (p.entries ? p.entries.length : 0), 0);
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
