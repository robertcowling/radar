/* flood-common.js — shared utilities for /warningexplorer + /floodwarnings
   Loaded via <script src="../flood-common.js"> before each page's own inline
   <script> (both pages live one directory below the repo root).
   Assumes `map` and `R2` are defined (as globals) by the time these
   functions are actually called, not necessarily by the time they're parsed. */

function esc(s) { return (s == null ? '' : String(s)).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function catLabel(cat) {
  return {river: 'River', coastal: 'Coastal & tidal', groundwater: 'Groundwater'}[cat] || cat;
}

const TYPE_SHORT = {'Flood Alert': 'Alert', 'Flood Warning': 'Warning', 'Severe Flood Warning': 'Severe'};
const TYPE_COL   = {'Flood Alert': '#f9ab00', 'Flood Warning': '#d93025', 'Severe Flood Warning': '#6d0019'};

/* Historical bulk-import events carry a T00:00 time component because the
   source spreadsheets were date-only (pandas defaults to midnight) — treat
   exact midnight as "no time recorded" rather than a real timestamp. Only
   live-appended events (from the fetch scripts) carry genuine times. */
function fmtEventDateTime(iso) {
  const m = /T(\d{2}):(\d{2})/.exec(iso);
  const d = new Date(iso);
  const ds = d.toLocaleDateString('en-GB', {day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC'});
  const hasRealTime = m && !(m[1] === '00' && m[2] === '00');
  if (!hasRealTime) return ds;
  const ts = d.toLocaleTimeString('en-GB', {hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC'});
  return `${ds}, ${ts}`;
}

const PROP_BUCKETS = [
  [50, '<50'],
  [100, '50–100'],
  [150, '100–150'],
  [200, '150–200'],
  [250, '200–250'],
  [300, '250–300'],
  [350, '300–350'],
  [400, '350–400'],
  [450, '400–450'],
  [500, '450–500'],
  [1000, '500–1,000'],
  [Infinity, '1,000+'],
];
function propRangeLabel(n) {
  if (n == null || n <= 0) return null;
  for (const [max, label] of PROP_BUCKETS) if (n < max) return label;
  return PROP_BUCKETS[PROP_BUCKETS.length - 1][1];
}

/* Sequential blue scale for the "colour by properties at risk" choropleth
   mode — index lines up 1:1 with PROP_BUCKETS. */
const PROP_COLORS = [
  '#eff6ff',
  '#dbeafe',
  '#bfdbfe',
  '#93c5fd',
  '#60a5fa',
  '#3b82f6',
  '#2563eb',
  '#1d4ed8',
  '#1e40af',
  '#1e3a8a',
  '#1c305c',
  '#152244',
];
function propColorIndex(n) {
  if (n == null || n <= 0) return -1;
  for (let i = 0; i < PROP_BUCKETS.length; i++) if (n < PROP_BUCKETS[i][0]) return i;
  return PROP_BUCKETS.length - 1;
}
function propColor(n) {
  const i = propColorIndex(n);
  return i < 0 ? '#cbd5e1' : PROP_COLORS[i];
}
function propChipHtml(n) {
  const range = propRangeLabel(n);
  if (!range) return '';
  const idx = propColorIndex(n);
  const bg = idx < 0 ? '#cbd5e1' : PROP_COLORS[idx];
  const fg = (idx >= 0 && idx <= 4) ? '#1f2937' : '#ffffff';
  const border = (idx === 0) ? 'border:1px solid #d1d5db;' : '';
  return `<span class="pop-prop-chip" style="background:${bg};color:${fg};${border}">${esc(range)}</span>`;
}

function getRarityInfo(hist, severityTier) {
  if (!hist) return null;
  if (hist.never_issued) return { tierLabel: 'Never issued', activeDescr: null, rateStr: null };
  const f = hist.freq && hist.freq[String(severityTier)];
  const tierLabel = f ? f.label : (hist.freq && hist.freq[String(hist.max_severity)] ? hist.freq[String(hist.max_severity)].label : null);
  const pct = f ? f.percentile : null;
  const rate = hist.per_year;
  const rateStr = rate >= 1 ? `~${rate.toFixed(1)} per year` : rate > 0 ? `~1 every ${Math.round(1 / rate)} years` : null;
  const activeDescr = pct != null ? (pct >= 75 ? `more active than ${Math.round(pct)}% of flood areas` : pct <= 25 ? `quieter than ${Math.round(100 - pct)}% of flood areas` : null) : null;
  return { tierLabel, activeDescr, rateStr };
}

function histBlock(hist, severityTier) {
  let h = `<div class="hist-block">`;
  h += `<div class="hist-title">Flood history</div>`;

  if (hist.never_issued) {
    h += `<div class="hist-sub">No flood alerts or warnings recorded here since Jan 2006</div>`;
    h += `</div>`;
    return h;
  }

  if (hist.merged_history) {
    const n = (hist.aliases || []).length;
    let note = `Includes history from ${n} retired area code${n === 1 ? '' : 's'}`;
    if (hist.former_names && hist.former_names.length) note += ` (formerly ${hist.former_names.map(esc).join(', ')})`;
    h += `<div class="hist-sub" style="color:#64748b">${note}</div>`;
  }

  const events = (hist.last_10 || []).slice(0, 10);
  if (hist.total_issued) h += `<div class="hist-sub">${hist.total_issued} events since ${hist.first_issued ? hist.first_issued.slice(0, 4) : '?'}</div>`;
  if (events.length) {
    h += `<div class="hist-sub">Last ${events.length} events</div>`;
    for (const e of events) {
      const col = TYPE_COL[e.t] || '#64748b';
      const short = TYPE_SHORT[e.t] || e.t;
      h += `<div class="hist-row"><span class="hist-dot" style="background:${col}"></span>`
         + `<span class="hist-date">${fmtEventDateTime(e.d)}</span>`
         + `<span class="hist-type" style="color:${col}">${esc(short)}</span></div>`;
    }
  }
  h += `</div>`;
  return h;
}

/* ---------- Tabbed popup (Details / History) ----------
   No DOM ids are used so this is safe even if multiple popup instances
   happen to exist in the DOM at once — tab switching is scoped via
   closest('.pop-wrap') on the clicked button. */
function tabbedPopup(detailsHtml, historyHtml) {
  const histContent = historyHtml || `<div class="hist-block"><div class="hist-title" style="margin-bottom:8px">Flood history</div><div style="font-size:11.5px;color:#94a3b8;margin-top:12px">No history recorded for this area.</div></div>`;
  return `<div class="pop-wrap">
    <div class="pop-tabs">
      <button class="pop-tab active" onclick="popSwitchTab(this,0)" type="button">Details</button>
      <button class="pop-tab" onclick="popSwitchTab(this,1)" type="button">History</button>
    </div>
    <div class="pop-panel" data-i="0">${detailsHtml}</div>
    <div class="pop-panel" data-i="1" style="display:none">${histContent}</div>
  </div>`;
}
function popSwitchTab(btn, i) {
  const wrap = btn.closest('.pop-wrap');
  wrap.querySelectorAll('.pop-tab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  wrap.querySelectorAll('.pop-panel').forEach(p => { p.style.display = (p.dataset.i === String(i)) ? 'block' : 'none'; });
}

/* ---------- Boundary overlays (catchments/counties/regions + rivers) ---------- */
const BFILES = {catchments: 'uk_catchments.geojson', counties: 'uk-counties.geojson', regions: 'uk_regions.geojson'};
const boundaryLayers = {};
let currentBoundary = 'none';
let riversVisible = false;

/* L.LayerGroup has no bringToBack() (only vector/Path layers do) — the
   boundary/river overlays are two-layer glow groups, so bring each child
   layer to back individually. Each bringToBack() call re-inserts its layer
   as the very first (bottom-most) child, so whichever call happens LAST
   ends up truly at the back — iterate in REVERSE of the group's creation
   order so the first-added child (the halo, meant to sit underneath)
   is called last and ends up at the bottom, preserving halo-under-line. */
function _bringGroupToBack(group) {
  if (!group) return;
  if (typeof group.bringToBack === 'function') { group.bringToBack(); return; }
  if (typeof group.eachLayer !== 'function') return;
  const layers = [];
  group.eachLayer(l => layers.push(l));
  for (let i = layers.length - 1; i >= 0; i--) {
    if (layers[i].bringToBack) layers[i].bringToBack();
  }
}

function onBoundary(val) {
  if (currentBoundary !== 'none' && boundaryLayers[currentBoundary]) map.removeLayer(boundaryLayers[currentBoundary]);
  currentBoundary = val;
  if (val === 'none' || !BFILES[val]) return;
  if (boundaryLayers[val]) { map.addLayer(boundaryLayers[val]); _bringGroupToBack(boundaryLayers[val]); return; }
  fetch(R2 + '/geo/' + BFILES[val]).then(r => { if (!r.ok) throw 0; return r.json(); })
    .catch(() => fetch('../' + BFILES[val]).then(r => r.json()))
    .then(d => {
      // Two-layer glow (light halo + grey line) rather than a CSS drop-shadow
      // filter — a white-on-white filter is invisible against a light
      // basemap, this renders correctly regardless of the tile style.
      const weight = val === 'regions' ? 2.4 : 1.8;
      boundaryLayers[val] = L.layerGroup([
        L.geoJSON(d, {style: {color: '#ffffff', weight: weight + 3, opacity: 0.9, fillOpacity: 0}, interactive: false}),
        L.geoJSON(d, {style: {color: '#5f6368', weight: weight, opacity: 0.9, fillOpacity: 0}, interactive: false}),
      ]);
      if (currentBoundary === val) { map.addLayer(boundaryLayers[val]); _bringGroupToBack(boundaryLayers[val]); }
    })
    .catch(e => console.error('boundary load failed', e));
}

function _strahlerOrder(props) {
  if (!props) return 1;
  const o = props['Strahler Stream Order'] || props.strahler || props.STRAHLER ||
            props.stream_order || props.STREAM_ORDER || props.StreamOrde || props.streamorde || 1;
  return parseInt(o) || 1;
}
function _riverWeight(order) {
  const w = [0.3, 0.3, 0.4, 0.6, 1.0, 1.7, 2.8];
  return w[Math.min(order - 1, 6)];
}

function toggleRivers() {
  const cb = document.getElementById('riverBtn');
  riversVisible = cb ? cb.checked : !riversVisible;
  if (!riversVisible) { if (boundaryLayers['rivers']) map.removeLayer(boundaryLayers['rivers']); return; }
  loadRivers();
}

/* Call after (re)adding the main polygon/marker layer to the map — a newly
   added layer always paints above earlier ones, so a filter change that
   rebuilds the poly layer would otherwise repaint it over rivers/boundaries
   that were previously sent to back. */
function reassertOverlayOrder() {
  if (currentBoundary !== 'none' && boundaryLayers[currentBoundary]) _bringGroupToBack(boundaryLayers[currentBoundary]);
  if (riversVisible && boundaryLayers['rivers']) _bringGroupToBack(boundaryLayers['rivers']);
}

function loadRivers() {
  if (boundaryLayers['rivers']) {
    if (riversVisible) { map.addLayer(boundaryLayers['rivers']); _bringGroupToBack(boundaryLayers['rivers']); }
    return;
  }
  fetch(R2 + '/geo/rivers.geojson').then(r => r.json()).then(d => {
    const gstyle = f => {
      const o = _strahlerOrder(f.properties);
      return { color: '#7ab8d4', weight: _riverWeight(o) * 1.8, opacity: 0.18, interactive: false };
    };
    const mstyle = f => {
      const o = _strahlerOrder(f.properties);
      return { color: '#1e6a96', weight: _riverWeight(o), opacity: 0.85, interactive: false };
    };
    boundaryLayers['rivers'] = L.layerGroup([
      L.geoJSON(d, { style: gstyle }),
      L.geoJSON(d, { style: mstyle })
    ]);
    if (riversVisible) { map.addLayer(boundaryLayers['rivers']); _bringGroupToBack(boundaryLayers['rivers']); }
  }).catch(e => console.error('rivers:', e));
}

/* ---------- "Show" dropdown (boundaries + rivers) ---------- */
function showToggle(e) {
  if (e) { e.stopPropagation(); e.preventDefault(); }
  document.getElementById('showPanel').classList.toggle('open');
}
document.addEventListener('click', e => {
  const dd = document.getElementById('showDropdown');
  const panel = document.getElementById('showPanel');
  if (dd && panel && panel.classList.contains('open') && !dd.contains(e.target)) panel.classList.remove('open');
});

