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

/* Property counts are shown as ranges, never exact numbers — the underlying
   spatial join is a useful estimate, not a precise figure. */
const PROP_BUCKETS = [
  [10, '<10'], [50, '10–50'], [200, '50–200'], [500, '200–500'],
  [1000, '500–1,000'], [5000, '1,000–5,000'], [20000, '5,000–20,000'],
  [Infinity, '20,000+'],
];
function propRangeLabel(n) {
  if (n == null || n <= 0) return null;
  for (const [max, label] of PROP_BUCKETS) if (n < max) return label;
  return '20,000+';
}

/* Sequential blue scale for the "colour by properties at risk" choropleth
   mode — index lines up 1:1 with PROP_BUCKETS. */
const PROP_COLORS = ['#eff6ff', '#dbeafe', '#bfdbfe', '#93c5fd', '#60a5fa', '#3b82f6', '#2563eb', '#1e3a8a'];
function propColorIndex(n) {
  if (n == null || n <= 0) return -1;
  for (let i = 0; i < PROP_BUCKETS.length; i++) if (n < PROP_BUCKETS[i][0]) return i;
  return PROP_BUCKETS.length - 1;
}
function propColor(n) {
  const i = propColorIndex(n);
  return i < 0 ? '#cbd5e1' : PROP_COLORS[i];
}

function histBlock(hist, severityTier) {
  const f = hist.freq && hist.freq[String(severityTier)];
  const tierLabel = f ? f.label : (hist.freq && hist.freq[String(hist.max_severity)] ? hist.freq[String(hist.max_severity)].label : null);
  const pct = f ? f.percentile : null;
  const rate = hist.per_year;
  const rateStr = rate >= 1 ? `~${rate.toFixed(1)} per year` : rate > 0 ? `~1 every ${Math.round(1 / rate)} years` : null;
  const activeDescr = pct != null ? (pct >= 75 ? `more active than ${Math.round(pct)}% of flood areas` : pct <= 25 ? `quieter than ${Math.round(100 - pct)}% of flood areas` : null) : null;
  const events = (hist.last_10 || []).slice(0, 10);

  let h = `<div class="hist-block">`;
  h += `<div class="hist-title">Flood history`;
  if (hist.total_issued) h += ` <span class="hist-muted">· ${hist.total_issued} events since ${hist.first_issued ? hist.first_issued.slice(0, 4) : '?'}</span>`;
  h += `</div>`;
  if (tierLabel) {
    h += `<div class="hist-rarity"><b>${esc(tierLabel)}</b>`;
    if (activeDescr) h += ` <span class="hist-muted">· ${activeDescr}</span>`;
    h += `</div>`;
  }
  if (rateStr) h += `<div class="hist-rate">${rateStr}</div>`;
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
  if (!historyHtml) return `<div class="pop-wrap"><div class="pop-panel">${detailsHtml}</div></div>`;
  return `<div class="pop-wrap">
    <div class="pop-tabs">
      <button class="pop-tab active" onclick="popSwitchTab(this,0)" type="button">Details</button>
      <button class="pop-tab" onclick="popSwitchTab(this,1)" type="button">History</button>
    </div>
    <div class="pop-panel" data-i="0">${detailsHtml}</div>
    <div class="pop-panel" data-i="1" style="display:none">${historyHtml}</div>
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
let riversVisible = true;

function onBoundary(val) {
  if (currentBoundary !== 'none' && boundaryLayers[currentBoundary]) map.removeLayer(boundaryLayers[currentBoundary]);
  currentBoundary = val;
  if (val === 'none' || !BFILES[val]) return;
  if (boundaryLayers[val]) { map.addLayer(boundaryLayers[val]); boundaryLayers[val].bringToBack(); return; }
  fetch('../' + BFILES[val]).then(r => { if (!r.ok) throw 0; return r.json(); })
    .catch(() => fetch(R2 + '/' + BFILES[val]).then(r => r.json()))
    .then(d => {
      boundaryLayers[val] = L.geoJSON(d, {style: {color: '#5f6368', weight: 1, fillOpacity: 0}, interactive: false});
      if (currentBoundary === val) { map.addLayer(boundaryLayers[val]); boundaryLayers[val].bringToBack(); }
    })
    .catch(e => console.error('boundary load failed', e));
}

function toggleRivers() {
  const cb = document.getElementById('riverBtn');
  riversVisible = cb ? cb.checked : !riversVisible;
  if (!riversVisible && boundaryLayers['rivers']) { map.removeLayer(boundaryLayers['rivers']); return; }
  loadRivers();
}

function loadRivers() {
  if (boundaryLayers['rivers']) {
    if (riversVisible) { map.addLayer(boundaryLayers['rivers']); boundaryLayers['rivers'].bringToBack(); }
    return;
  }
  fetch(R2 + '/geo/rivers.geojson').then(r => r.json()).then(d => {
    boundaryLayers['rivers'] = L.geoJSON(d, {
      style: f => {
        const order = f.properties['Strahler Stream Order'] || 1;
        return {color: '#3b82f6', weight: order >= 6 ? 2 : order >= 4 ? 1.2 : 0.6, opacity: order >= 4 ? 0.7 : 0.4};
      },
      interactive: false,
    });
    if (riversVisible) { map.addLayer(boundaryLayers['rivers']); boundaryLayers['rivers'].bringToBack(); }
  }).catch(e => console.error('rivers:', e));
}

/* ---------- Find-a-place geocoder (floating pill widget) ---------- */
let geoPin = null;

function geoExpand() {
  document.getElementById('geoFloat').classList.add('expanded');
  document.getElementById('geoInput').focus();
}
function geoCollapse() {
  document.getElementById('geoFloat').classList.remove('expanded');
  document.getElementById('geoDrop').classList.remove('open');
}
function geoIconClick(e) {
  e.stopPropagation();
  const el = document.getElementById('geoFloat');
  if (!el.classList.contains('expanded')) geoExpand();
  else geoSearch();
}
function geoClear(e) {
  e.stopPropagation();
  document.getElementById('geoInput').value = '';
  document.getElementById('geoDrop').classList.remove('open');
  document.getElementById('geoInput').focus();
}

function geoSearch() {
  const q = document.getElementById('geoInput').value.trim();
  if (!q) return;
  const drop = document.getElementById('geoDrop');
  drop.innerHTML = '<div class="geo-result2" style="color:#94a3b8">Searching…</div>';
  drop.classList.add('open');
  fetch('https://nominatim.openstreetmap.org/search?q=' + encodeURIComponent(q) +
    '&format=json&limit=5&countrycodes=gb', {headers: {'Accept-Language': 'en'}})
    .then(r => r.json())
    .then(results => {
      if (!results.length) {
        drop.innerHTML = '<div class="geo-result2" style="color:#94a3b8">No results found</div>';
        return;
      }
      if (results.length === 1) { geoGoto(results[0]); return; }
      drop.innerHTML = '';
      results.forEach(res => {
        const el = document.createElement('div');
        el.className = 'geo-result2';
        el.textContent = res.display_name.split(',').slice(0, 3).join(', ');
        el.onclick = () => geoGoto(res);
        drop.appendChild(el);
      });
    })
    .catch(() => { drop.innerHTML = '<div class="geo-result2" style="color:#94a3b8">Search failed</div>'; });
}

function geoGoto(result) {
  document.getElementById('geoDrop').classList.remove('open');
  if (geoPin) { map.removeLayer(geoPin); geoPin = null; }
  const lat = parseFloat(result.lat), lon = parseFloat(result.lon);
  const bb = result.boundingbox;
  if (bb) map.fitBounds([[+bb[0], +bb[2]], [+bb[1], +bb[3]]], {maxZoom: 13});
  else map.setView([lat, lon], 12);
  geoPin = L.circleMarker([lat, lon], {radius: 8, color: '#1d4ed8', weight: 2,
    fillColor: '#3b82f6', fillOpacity: 0.9}).addTo(map)
    .bindTooltip(result.display_name.split(',').slice(0, 2).join(', '), {permanent: false, className: 'ff-tip'});
  geoPin.on('click', () => { map.removeLayer(geoPin); geoPin = null; });
}

document.addEventListener('click', e => {
  if (!e.target.closest('.geo-float')) geoCollapse();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') geoCollapse();
});
