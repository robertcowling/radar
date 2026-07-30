import re

with open('ukv_trends.html', 'r', encoding='utf-8') as f:
    html = f.read()

# 1. Update the HTML playerPanel block
player_panel_old = re.search(r'<div class="player-panel surface".*?</div>\s*</div>', html, re.DOTALL)
if not player_panel_old:
    print("Could not find playerPanel in HTML")
    exit(1)

player_panel_new = """<div class="player-panel surface" id="playerPanel" style="display:none;">
  <div class="control-group" style="min-width: 340px;">
    <div style="display:flex; justify-content:space-between; width:100%; align-items:center; margin-bottom: 2px;">
      <span class="control-label">Accumulation Window</span>
      <span id="windowDisplayLabel" style="font-size:11px; font-weight:700; color:#1a73e8; background:#e8f0fe; padding:2px 8px; border-radius:12px; white-space:nowrap;">Day 1 (T+0h to T+24h)</span>
    </div>
    <!-- Quick Presets -->
    <div class="seg" id="winSeg" style="flex-wrap:wrap; gap:2px;">
      <button class="seg-btn"        data-win="6h"   onclick="setWinPreset('6h')">6h</button>
      <button class="seg-btn"        data-win="12h"  onclick="setWinPreset('12h')">12h</button>
      <button class="seg-btn active" data-win="day1" onclick="setWinPreset('day1')">Day 1</button>
      <button class="seg-btn"        data-win="day2" onclick="setWinPreset('day2')">Day 2</button>
      <button class="seg-btn"        data-win="day3" onclick="setWinPreset('day3')">Day 3</button>
      <button class="seg-btn"        data-win="48h"  onclick="setWinPreset('48h')">48h</button>
      <button class="seg-btn"        data-win="72h"  onclick="setWinPreset('72h')">72h</button>
      <button class="seg-btn"        data-win="full" onclick="setWinPreset('full')">Full 120h</button>
    </div>
    <!-- Continuous Slider Controls -->
    <div style="display:flex; gap:12px; width:100%; margin-top:6px; align-items:center; background:#f8fafc; padding:6px 10px; border-radius:8px; border:1px solid #e2e8f0;">
      <div style="flex:1; display:flex; flex-direction:column; gap:2px;">
        <div style="display:flex; justify-content:space-between; font-size:10px; color:#64748b; font-weight:600;">
          <span>Start Offset:</span>
          <b id="lblStartHr" style="color:#0f172a;">T+0h</b>
        </div>
        <input type="range" id="sliderStart" min="0" max="114" step="1" value="0" oninput="onSliderChange()" style="width:100%; cursor:pointer; height:4px; accent-color:#1a73e8;">
      </div>
      <div style="flex:1; display:flex; flex-direction:column; gap:2px;">
        <div style="display:flex; justify-content:space-between; font-size:10px; color:#64748b; font-weight:600;">
          <span>Window Length:</span>
          <b id="lblLenHr" style="color:#0f172a;">24h</b>
        </div>
        <input type="range" id="sliderLen" min="1" max="120" step="1" value="24" oninput="onSliderChange()" style="width:100%; cursor:pointer; height:4px; accent-color:#1a73e8;">
      </div>
    </div>
  </div>

  <div class="control-group">
    <span class="control-label">View</span>
    <div class="seg" id="viewSeg">
      <button id="btnGrid" class="seg-btn"        onclick="setViewMode('grid')">Grid</button>
      <button id="btnArea" class="seg-btn active" onclick="setViewMode('area')">Areas</button>
    </div>
  </div>

  <div class="control-group" id="boundaryControl">
    <span class="control-label">Areas</span>
    <div class="seg" id="layerSeg">
      <button class="seg-btn active" data-layer="catchments" onclick="setLayer('catchments')">Catchments</button>
      <button class="seg-btn"        data-layer="regions"    onclick="setLayer('regions')">Regions</button>
      <button class="seg-btn"        data-layer="grid"       onclick="setLayer('grid')">Grid 20km</button>
    </div>
  </div>

  <div class="control-group">
    <span class="control-label">Colour scale</span>
    <select id="schemeSelect" onchange="setScheme(this.value)">
      <option value="norm">Normal</option>
      <option value="high">High events</option>
      <option value="met" selected>Alternative (Met Office)</option>
    </select>
  </div>
</div>"""

html = html.replace(player_panel_old.group(0), player_panel_new)

# 2. Update Script section with continuous window engine
script_start = html.find('<script>')
script_end = html.find('</script>')
if script_start == -1 or script_end == -1:
    print("Could not find script tags")
    exit(1)

new_script = r"""
var GEO_FILES = {
  catchments: { file: 'ukv_catchments.geojson', key: 'name' },
  regions:    { file: 'ukv_regions.geojson',    key: 'name' },
  grid:       { file: 'ukv_grid_20km.geojson',  key: 'id' }
};
var R2_BASE = 'https://pub-96089466ef9841fb90f34b6f89f0a090.r2.dev';
var R2_CDN  = 'https://radar.floodforecast.co.uk';

var _data = null, _meta = null;
var _geoCache = {}, _tsCache = {};
var _dynamicComp = null;

var _winStartA = 0, _winEndA = 24, _winKey = 'day1';
var _layer = 'catchments', _scheme = 'norm';
var _viewMode = 'area';
var _compIdx = 0;
var _selectedRunLeft = null;
var _selectedRunRight = null;
var _showAll = false;

var _mapPrior = null, _mapLatest = null, _mapDiff = null;
var _polyPrior = null, _polyLatest = null, _polyDiff = null;
var _rasterPrior = null, _rasterLatest = null, _rasterDiff = null;
var _syncing = false;

var LAYER_LABEL = { catchments: 'Catchment', regions: 'Region', grid: '20km Grid Cell' };
var WIN_PRESETS = {
  '6h':   { name: '0–6h',         start: 0,  end: 6 },
  '12h':  { name: '0–12h',        start: 0,  end: 12 },
  'day1': { name: 'Day 1 (24h)',  start: 0,  end: 24 },
  'day2': { name: 'Day 2 (24h)',  start: 24, end: 48 },
  'day3': { name: 'Day 3 (24h)',  start: 48, end: 72 },
  '48h':  { name: '0–48h',        start: 0,  end: 48 },
  '72h':  { name: '0–72h',        start: 0,  end: 72 },
  'full': { name: 'Full (120h)',  start: 0,  end: 120 }
};

var BIAS_BOUNDS = [-200, -100, -50, -25, -10, 10, 25, 50, 100, 200];
var BIAS_COLORS = ['#053061','#2166ac','#4393c3','#92c5de','#d1e5f0','#f7f7f7','#fddbc7','#f4a582','#d6604d','#b2182b','#67001f'];

var SCHEMES = {
  norm: {
    bounds: [0.5, 1, 2, 5, 10, 20, 40, 80, 160],
    colors: ['#e0f3ff','#81d4fa','#03a9f4','#01579b','#2e7d32','#fbc02d','#ef6c00','#c62828','#6a1b9a']
  },
  high: {
    bounds: [2, 5, 10, 25, 50, 100, 150, 200, 350],
    colors: ['#e0f3ff','#81d4fa','#03a9f4','#01579b','#2e7d32','#fbc02d','#ef6c00','#c62828','#6a1b9a']
  },
  met: {
    bounds: [0.03, 1, 5, 10, 20, 40, 60, 80, 100, 120, 140, 160, 180],
    colors: ['#3A6CFF','#00FF00','#FFFF95','#FFD563','#FF9618','#E86100','#BA2000','#CC537D','#DB92DC','#FF02FF','#FFFFFF','#C8C8C8','#BFBF00']
  }
};

function openInsightsModal() { document.getElementById('insightsModalBackdrop').classList.add('open'); }
function closeInsightsModal() { document.getElementById('insightsModalBackdrop').classList.remove('open'); }
function openTableModal() { document.getElementById('tableModalBackdrop').classList.add('open'); }
function closeTableModal() { document.getElementById('tableModalBackdrop').classList.remove('open'); }

function esc(s) {
  return String(s).replace(/[&<>\"]/g, function(c) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c];
  });
}
function fmtDelta(d) {
  if (d === null || d === undefined) return '—';
  return (d >= 0 ? '+' : '') + d.toFixed(1) + 'mm';
}
function fmtMm(v) { return (v === null || v === undefined) ? '—' : v.toFixed(1) + 'mm'; }

function biasFillColor(mm) {
  if (mm == null) return null;
  for (var i = 0; i < BIAS_BOUNDS.length; i++) {
    if (mm < BIAS_BOUNDS[i]) return BIAS_COLORS[i];
  }
  return BIAS_COLORS[BIAS_BOUNDS.length];
}

function bucketColorForScheme(v, schemeKey) {
  if (v === null || v === undefined) return null;
  var s = SCHEMES[schemeKey || _scheme];
  if (v < s.bounds[0]) return null;
  for (var i = s.bounds.length - 1; i >= 0; i--) {
    if (v >= s.bounds[i]) return s.colors[i];
  }
  return null;
}

function getWinLabel() {
  if (_winKey && _winKey !== 'custom' && WIN_PRESETS[_winKey]) {
    return WIN_PRESETS[_winKey].name + ' (T+' + _winStartA + 'h to T+' + _winEndA + 'h)';
  }
  var len = _winEndA - _winStartA;
  return 'Custom ' + len + 'h (T+' + _winStartA + 'h to T+' + _winEndA + 'h)';
}

/* ── Data access ─────────────────────────────────────────────────────── */
function currentComp() { return _dynamicComp || (_data.comparisons || [])[_compIdx] || {}; }
function currentRows() {
  var c = currentComp();
  var w = (c.windows && c.windows[_winKey]) || {};
  return w[_layer] || {};
}
function valueFor(name, mode) {
  var r = currentRows()[name];
  if (!r) return null;
  if (mode === 'latest') return r.run_a_mm;
  if (mode === 'prior')  return r.run_b_mm;
  return r.delta_mm;
}

/* ── Insights & Rules ────────────────────────────────────────────────── */
function renderInsights(insights) {
  var el = document.getElementById('insightsBody');
  if (!insights || !insights.length) {
    el.innerHTML = '<p style="font-size:13px;color:#64748b;">No significant run-to-run changes in this update.</p>';
    return;
  }
  el.innerHTML = insights.map(function(t) {
    return '<div class="insight-row"><span class="insight-dot">•</span><span>' + esc(t) + '</span></div>';
  }).join('');
}

function renderRules(rules) {
  rules = rules || {};
  var cf = rules.catchment_floor_mm != null ? rules.catchment_floor_mm : 2;
  var gf = rules.grid_floor_mm != null ? rules.grid_floor_mm : 5;
  var th = (rules.thresholds_mm || [10, 25, 50, 100]).join(', ');
  var pc = rules.relative_change_pct != null ? rules.relative_change_pct : 20;
  document.getElementById('rulesBody').innerHTML =
    '<div class="rules-note">' +
      'Areas are compared at the <b>same valid time</b> across runs (so we are comparing like for like, ' +
      'not just two different forecast hours). A change is flagged as <b>significant</b> only when ' +
      '<b>both</b> of these hold:' +
      '<ul>' +
        '<li><b>It clears the noise floor</b> — the change exceeds <code>' + cf + ' mm</code> for catchments / regions, ' +
            'or <code>' + gf + ' mm</code> for 20 km grid cells.</li>' +
        '<li><b>And it matters operationally</b> — the total crosses a flood-relevant threshold ' +
            '(<code>' + th + ' mm</code>/24h), <i>or</i> it shifts the prior total by more than <code>' + pc + '%</code>.</li>' +
      '</ul>' +
      'Significant rows are marked with a coloured dot (' +
      '<span style="color:#b91c1c;font-weight:700;">●</span> wetter, ' +
      '<span style="color:#1565c0;font-weight:700;">●</span> drier) and the reason ' +
      '(<i>threshold</i> = crossed a flood band, <i>relative</i> = ≥' + pc + '% shift).' +
    '</div>';
}

/* ── Dynamic Computation ─────────────────────────────────────────────── */
function parseRunTs(ts) {
  var y = ts.substring(0,4), m = ts.substring(4,6), d = ts.substring(6,8);
  var H = ts.substring(9,11), M = ts.substring(11,13);
  return new Date(Date.UTC(y, m-1, d, H, M));
}

async function loadRunAreaTs(run_ts) {
  if (_tsCache[run_ts]) return _tsCache[run_ts];
  try {
    var r = await fetch(R2_CDN + '/ukv_area_ts/' + run_ts + '.json');
    if (!r.ok) return null;
    var d = await r.json();
    _tsCache[run_ts] = d;
    return d;
  } catch (e) {
    return null;
  }
}

function getRunLabel(ts) {
  if (_meta && _meta.runs) {
    var r = _meta.runs.find(function(x) { return x.run_ts === ts; });
    if (r) return r.run_label;
  }
  return ts;
}

function computeWindowForLayer(tsA, tsB, startA, endA, lag_h, lyr, rules) {
  rules = rules || (_data && _data.significance_rules) || {};
  var FLOORS = { 'catchments': rules.catchment_floor_mm || 2, 'regions': rules.catchment_floor_mm || 2, 'grid': rules.grid_floor_mm || 5 };
  var floor = FLOORS[lyr] || 2;
  
  var startB = startA + lag_h;
  var endB = endA + lag_h;
  
  var areasA = tsA ? (tsA[lyr] || {}) : {};
  var areasB = tsB ? (tsB[lyr] || {}) : {};
  
  var keys = Object.keys(areasA);
  if (keys.length === 0) keys = Object.keys(areasB);
  
  var layer_deltas = {};
  for (var i = 0; i < keys.length; i++) {
    var name = keys[i];
    var arrA = areasA[name], arrB = areasB[name];
    
    var sumA = null, sumB = null;
    if (arrA && endA <= arrA.length && startA >= 0) {
      sumA = 0; for (var k = startA; k < endA; k++) sumA += arrA[k];
    }
    if (arrB && endB <= arrB.length && startB >= 0) {
      sumB = 0; for (var k = startB; k < endB; k++) sumB += arrB[k];
    }
    
    if (sumA !== null || sumB !== null) {
      var va = sumA !== null ? Math.round(sumA * 100) / 100 : null;
      var vb = sumB !== null ? Math.round(sumB * 100) / 100 : null;
      var delta = (va !== null && vb !== null) ? Math.round((va - vb) * 100) / 100 : null;
      
      var sig = false, reason = null;
      if (delta !== null && Math.abs(delta) >= floor) {
        var tlist = rules.thresholds_mm || [10, 25, 50, 100];
        for (var j = 0; j < tlist.length; j++) {
          var t = tlist[j];
          if ((va >= t && vb < t) || (va < t && vb >= t)) { sig = true; reason = 'threshold'; break; }
        }
        if (!sig && vb > 0) {
          var pc = (rules.relative_change_pct || 20) / 100.0;
          var shift = Math.abs(delta) / vb;
          if (shift >= pc) { sig = true; reason = 'relative'; }
        }
      }
      layer_deltas[name] = { delta_mm: delta, run_a_mm: va, run_b_mm: vb, significant: sig, reason: reason };
    }
  }
  return layer_deltas;
}

async function buildDynamicComp(runA, runB) {
  var tsA = await loadRunAreaTs(runA);
  var tsB = await loadRunAreaTs(runB);

  var dtA = parseRunTs(runA), dtB = parseRunTs(runB);
  var lag_h = Math.round((dtA - dtB) / 3600000);

  var comp = {
    run_a: runA, run_a_label: getRunLabel(runA),
    run_b: runB, run_b_label: getRunLabel(runB),
    label: (lag_h < 0 ? '+' : '-') + Math.abs(lag_h) + 'h run',
    windows: {}
  };

  comp.windows[_winKey] = {};
  ['catchments', 'regions', 'grid'].forEach(function(lyr) {
    comp.windows[_winKey][lyr] = computeWindowForLayer(tsA, tsB, _winStartA, _winEndA, lag_h, lyr);
  });

  var catchDeltas = comp.windows[_winKey]['catchments'] || {};
  var sigCatch = Object.keys(catchDeltas).filter(function(k) { return catchDeltas[k].significant; });
  var insights = [];
  if (sigCatch.length > 0) {
    var wetter = sigCatch.filter(function(k) { return catchDeltas[k].delta_mm > 0; }).length;
    var drier = sigCatch.filter(function(k) { return catchDeltas[k].delta_mm < 0; }).length;
    insights.push(sigCatch.length + ' catchments show significant changes in this window (' + wetter + ' wetter, ' + drier + ' drier).');
  } else {
    insights.push('No significant catchment-level changes detected in this window.');
  }
  comp.insights = insights;
  return comp;
}

/* ── Window Control Handlers ─────────────────────────────────────────── */
function updateWindowUI() {
  document.getElementById('windowDisplayLabel').textContent = getWinLabel();
  document.getElementById('lblStartHr').textContent = 'T+' + _winStartA + 'h';
  document.getElementById('lblLenHr').textContent = (_winEndA - _winStartA) + 'h';
  document.getElementById('sliderStart').value = _winStartA;
  document.getElementById('sliderLen').value = _winEndA - _winStartA;

  document.querySelectorAll('#winSeg .seg-btn').forEach(function(b) {
    b.classList.toggle('active', b.dataset.win === _winKey);
  });
}

function setWinPreset(key) {
  _winKey = key;
  if (WIN_PRESETS[key]) {
    _winStartA = WIN_PRESETS[key].start;
    _winEndA = WIN_PRESETS[key].end;
  }
  updateWindowUI();
  applyComparisonSelection();
}

function onSliderChange() {
  _winKey = 'custom';
  var s = parseInt(document.getElementById('sliderStart').value, 10) || 0;
  var l = parseInt(document.getElementById('sliderLen').value, 10) || 24;
  if (s + l > 120) l = 120 - s;
  _winStartA = s;
  _winEndA = s + l;
  updateWindowUI();
  applyComparisonSelection();
}

/* ── Master-Detail Top Bar Selectors for Left and Centre Panels ─────────── */
function renderHeaderSelects() {
  var sLeft  = document.getElementById('selectRunLeft');
  var sRight = document.getElementById('selectRunRight');
  if (!sLeft || !sRight || !_data || !_meta) return;

  var allRuns = _meta.runs || [];
  if (!_selectedRunLeft) _selectedRunLeft = _data.latest_run || (allRuns[0] && allRuns[0].run_ts);
  
  if (!_selectedRunRight) {
    var existing = (_data.comparisons || []).find(function(c) { return c.run_a === _selectedRunLeft && c.label === '-12h run'; });
    if (!existing) existing = (_data.comparisons || [])[0];
    if (existing) {
      _selectedRunRight = existing.run_b;
    } else {
      var leftIdx = allRuns.findIndex(function(r) { return r.run_ts === _selectedRunLeft; });
      var rIdx = leftIdx >= 0 && leftIdx + 1 < allRuns.length ? leftIdx + 1 : 0;
      if (allRuns[rIdx]) _selectedRunRight = allRuns[rIdx].run_ts;
    }
  }

  sLeft.innerHTML = allRuns.map(function(r) {
    var sel = (r.run_ts === _selectedRunLeft) ? ' selected' : '';
    return '<option value="' + esc(r.run_ts) + '"' + sel + '>' + esc(r.run_label) + '</option>';
  }).join('');

  sRight.innerHTML = allRuns.map(function(r) {
    if (r.run_ts === _selectedRunLeft) return '';
    var sel = (r.run_ts === _selectedRunRight) ? ' selected' : '';
    var dtA = parseRunTs(_selectedRunLeft);
    var dtB = parseRunTs(r.run_ts);
    var lag_h = Math.round((dtA - dtB) / 3600000);
    var lbl = esc(r.run_label) + ' (' + (lag_h < 0 ? '+' : '-') + Math.abs(lag_h) + 'h)';
    return '<option value="' + esc(r.run_ts) + '"' + sel + '>' + lbl + '</option>';
  }).join('');
}

function onRunSelectChange(side) {
  var sLeft  = document.getElementById('selectRunLeft');
  var sRight = document.getElementById('selectRunRight');
  if (!sLeft || !sRight) return;

  _selectedRunLeft = sLeft.value;
  _selectedRunRight = sRight.value;
  
  if (_selectedRunLeft === _selectedRunRight) {
     var opts = Array.from(sRight.options);
     if (opts.length > 0) _selectedRunRight = opts[0].value;
  }
  
  applyComparisonSelection();
}

async function applyComparisonSelection() {
  var comp = await buildDynamicComp(_selectedRunLeft, _selectedRunRight);
  if (!comp) {
    document.getElementById('tableWrap').innerHTML = '<p class="empty-msg">Failed to compute comparison.</p>';
    return;
  }
  _dynamicComp = comp;
  renderHeaderSelects();
  renderInsights(currentComp().insights || _data.insights);
  refreshPolyStyles(); renderLegend(); updateRaster();
  renderTable();
}

/* ── Map ─────────────────────────────────────────────────────────────── */
function initMap() {
  var INIT_CENTER = [54.5, -3.5], INIT_ZOOM = 5;
  var TILE_URL  = 'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png';
  var TILE_OPTS = { maxZoom: 12, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/">CARTO</a>' };

  _mapPrior  = L.map('mapPrior',  { zoomControl: true }).setView(INIT_CENTER, INIT_ZOOM);
  _mapLatest = L.map('mapLatest', { zoomControl: false }).setView(INIT_CENTER, INIT_ZOOM);
  _mapDiff   = L.map('mapDiff',   { zoomControl: false }).setView(INIT_CENTER, INIT_ZOOM);

  [_mapPrior, _mapLatest, _mapDiff].forEach(function(m) {
    L.tileLayer(TILE_URL, TILE_OPTS).addTo(m);
  });

  function sync(src) {
    if (_syncing) return;
    _syncing = true;
    var c = src.getCenter(), z = src.getZoom();
    [_mapPrior, _mapLatest, _mapDiff].forEach(function(m) {
      if (m && m !== src) m.setView(c, z, { animate: false });
    });
    _syncing = false;
  }

  _mapPrior.on('moveend',  function() { sync(_mapPrior); });
  _mapLatest.on('moveend', function() { sync(_mapLatest); });
  _mapDiff.on('moveend',   function() { sync(_mapDiff); });
}

function loadGeo(layer) {
  if (_geoCache[layer]) return Promise.resolve(_geoCache[layer]);
  var info = GEO_FILES[layer];
  return fetch(info.file)
    .then(function(r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(function(g) { _geoCache[layer] = g; return g; });
}

function styleFeature(feature, mode) {
  var info = GEO_FILES[_layer];
  var name = feature.properties[info.key];
  var v = valueFor(name, mode);
  if (_viewMode === 'grid') {
    return { color: '#475569', weight: 0.5, fillColor: '#000000', fillOpacity: 0.0 };
  }
  var color = (mode === 'diff')
    ? biasFillColor(v)
    : bucketColorForScheme(v, _scheme);
  if (color === null) {
    return { color: '#94a3b8', weight: 0.5, fillColor: '#cbd5e1', fillOpacity: 0.05 };
  }
  return { color: '#334155', weight: 0.6, fillColor: color, fillOpacity: 0.72 };
}

function tooltipHtml(name) {
  var r = currentRows()[name];
  var comp = currentComp();
  if (!r) return '<div class="poly-tip"><b>' + esc(name) + '</b><br>No data this window.</div>';
  var d = r.delta_mm;
  var dCls = d > 0 ? 'pt-delta-pos' : (d < 0 ? 'pt-delta-neg' : '');
  return '<div class="poly-tip"><b>' + esc(name) + '</b><br>' +
    esc(comp.run_a_label || 'Latest') + ': ' + fmtMm(r.run_a_mm) + '<br>' +
    esc(comp.run_b_label || 'Comparison') + ': ' + fmtMm(r.run_b_mm) + '<br>' +
    'Change: <span class="' + dCls + '">' + fmtDelta(d) + '</span>' +
    (r.significant ? ' ●' : '') +
    '</div>';
}

function drawPolys() {
  if (!_mapPrior || !_mapLatest || !_mapDiff) return;
  loadGeo(_layer).then(function(geo) {
    if (_polyPrior)  { _mapPrior.removeLayer(_polyPrior);   _polyPrior = null; }
    if (_polyLatest) { _mapLatest.removeLayer(_polyLatest); _polyLatest = null; }
    if (_polyDiff)   { _mapDiff.removeLayer(_polyDiff);     _polyDiff = null; }

    var info = GEO_FILES[_layer];

    function createPolyLayer(mapInst, mode) {
      return L.geoJSON(geo, {
        style: function(f) { return styleFeature(f, mode); },
        onEachFeature: function(feature, lyr) {
          var name = feature.properties[info.key];
          lyr.bindTooltip(function() { return tooltipHtml(name); }, { sticky: true });
        }
      }).addTo(mapInst);
    }

    _polyPrior  = createPolyLayer(_mapPrior,  'latest');
    _polyLatest = createPolyLayer(_mapLatest, 'prior');
    _polyDiff   = createPolyLayer(_mapDiff,   'diff');

    try {
      var b = _polyPrior.getBounds();
      if (b.isValid()) {
        [_mapPrior, _mapLatest, _mapDiff].forEach(function(m) {
          m.fitBounds(b, { padding: [15, 15], maxZoom: 8 });
        });
      }
    } catch (e) {}
  }).catch(function() {});
}

function refreshPolyStyles() {
  if (_polyPrior)  _polyPrior.setStyle(function(f) { return styleFeature(f, 'latest'); });
  if (_polyLatest) _polyLatest.setStyle(function(f) { return styleFeature(f, 'prior'); });
  if (_polyDiff)   _polyDiff.setStyle(function(f) { return styleFeature(f, 'diff'); });
}

function renderLegend() {
  var comp = currentComp();
  
  var dTitle = document.getElementById('headerDiffTitle');
  if (dTitle) dTitle.textContent = 'Difference';
  
  var pSub = document.getElementById('headerPriorSub');
  var lSub = document.getElementById('headerLatestSub');
  var dSub = document.getElementById('headerDiffSub');
  var winTxt = getWinLabel();
  if (pSub) pSub.textContent = 'Run total (' + winTxt + ')';
  if (lSub) lSub.textContent = 'Run total (' + winTxt + ')';
  if (dSub) dSub.textContent = 'Left minus Centre';

  function buildAccumCbarHtml(title) {
    var s = SCHEMES[_scheme];
    var blocks = s.colors.map(function(c) {
      return '<div class="cbar-block" style="background:' + c + ';"></div>';
    }).join('');
    var n = s.colors.length;
    var labels = s.bounds.map(function(v, i) {
      var pct = ((i + 0.5) / n * 100).toFixed(1);
      var valStr = v < 1 ? v.toString().replace('0.', '.') : Math.round(v);
      return '<span class="cbar-label" style="left:' + pct + '%;">' + valStr + '</span>';
    }).join('');
    return '<p>' + title + '</p>' +
      '<div class="cbar">' + blocks + '</div>' +
      '<div class="cbar-labels">' + labels + '</div>';
  }

  function buildDiffCbarHtml() {
    var blocks = BIAS_COLORS.map(function(c) {
      return '<div class="cbar-block" style="background:' + c + ';"></div>';
    }).join('');
    var n = BIAS_COLORS.length; // 11
    var labels = BIAS_BOUNDS.map(function(v, i) {
      var pct = i < 5 ? (i + 0.5) / n * 100 : (i + 1.5) / n * 100;
      return '<span class="cbar-label" style="left:' + pct.toFixed(1) + '%;">' + (v > 0 ? '+' : '') + Math.round(v) + '</span>';
    }).join('');
    labels += '<span class="cbar-label" style="left:' + (5.5 / n * 100).toFixed(1) + '%;font-weight:600;">±2</span>';
    return '<p>Difference (mm)</p>' +
      '<div class="cbar">' + blocks + '</div>' +
      '<div class="cbar-labels">' + labels + '</div>' +
      '<div class="bias-arrows">' +
        '<span style="color:#b91c1c;font-weight:600;">◄ Drier (Centre higher)</span>' +
        '<span style="color:#2166ac;font-weight:600;">Wetter (Left higher) ►</span>' +
      '</div>';
  }

  var elP = document.getElementById('legendPrior');
  var elL = document.getElementById('legendLatest');
  var elD = document.getElementById('legendDiff');
  if (elP) elP.innerHTML = buildAccumCbarHtml('Accumulation (' + winTxt + ')');
  if (elL) elL.innerHTML = buildAccumCbarHtml('Accumulation (' + winTxt + ')');
  if (elD) elD.innerHTML = buildDiffCbarHtml();
}

function updateRaster() {
  [_mapPrior, _mapLatest, _mapDiff].forEach(function(m, i) {
    var vName = i === 0 ? '_rasterPrior' : (i === 1 ? '_rasterLatest' : '_rasterDiff');
    if (window[vName] && m) { m.removeLayer(window[vName]); window[vName] = null; }
  });
  if (_viewMode !== 'grid' || !_data || !_data.image_bounds) return;
  var comp = currentComp();
  var urlA = (comp.raw_image_a_url || '').replace(R2_BASE, R2_CDN);
  var urlB = (comp.raw_image_b_url || '').replace(R2_BASE, R2_CDN);
  var urlD = (comp.diff_image_url || '').replace(R2_BASE, R2_CDN);

  if (urlA && _mapPrior) {
    _rasterPrior = L.imageOverlay(urlA, _data.image_bounds, { opacity: 0.8, interactive: false }).addTo(_mapPrior);
  }
  if (urlB && _mapLatest) {
    _rasterLatest = L.imageOverlay(urlB, _data.image_bounds, { opacity: 0.8, interactive: false }).addTo(_mapLatest);
  }
  if (urlD && _mapDiff) {
    _rasterDiff = L.imageOverlay(urlD, _data.image_bounds, { opacity: 0.8, interactive: false }).addTo(_mapDiff);
  }
}

function setViewMode(mode) {
  _viewMode = mode;
  document.getElementById('btnGrid').classList.toggle('active', mode === 'grid');
  document.getElementById('btnArea').classList.toggle('active', mode === 'area');
  var bControl = document.getElementById('boundaryControl');
  if (bControl) bControl.style.opacity = mode === 'grid' ? '0.5' : '1';
  refreshPolyStyles();
  updateRaster();
}

function setScheme(s) {
  _scheme = s;
  refreshPolyStyles();
  renderLegend();
}

/* ── Table ───────────────────────────────────────────────────────────── */
function renderTable() {
  var wLbl = document.getElementById('tblWinLabel');
  var lLbl = document.getElementById('tblLayerLabel');
  if (wLbl) wLbl.textContent = getWinLabel();
  if (lLbl) lLbl.textContent = LAYER_LABEL[_layer];

  var comp = currentComp();
  var rows = currentRows();
  var keys = Object.keys(rows);

  keys.sort(function(a, b) {
    var sa = rows[a].significant ? 1 : 0, sb = rows[b].significant ? 1 : 0;
    if (sa !== sb) return sb - sa;
    return Math.abs(rows[b].delta_mm || 0) - Math.abs(rows[a].delta_mm || 0);
  });

  var sigKeys = keys.filter(function(k) { return rows[k].significant; });
  var display = (_showAll || sigKeys.length === 0) ? keys : sigKeys;

  var html = '';
  if (display.length === 0) {
    html = '<p class="empty-msg">No data for this window / layer combination.</p>';
  } else {
    html = '<div style="overflow-x:auto;"><table class="data-table"><thead><tr>' +
      '<th>Area</th>' +
      '<th>' + esc(comp.run_a_label || 'Left run') + '</th>' +
      '<th>' + esc(comp.run_b_label || 'Centre run') + '</th>' +
      '<th>Change</th><th>Flagged by</th><th></th></tr></thead><tbody>';
    display.forEach(function(k) {
      var r = rows[k];
      var d = r.delta_mm;
      var cls = d > 0 ? 'delta-pos' : (d < 0 ? 'delta-neg' : '');
      var dotCls = r.significant ? (d > 0 ? 'sig-dot wetter' : 'sig-dot drier') : '';
      var reason = r.significant && r.reason
        ? '<span class="reason-pill">' + (r.reason === 'threshold' ? 'Flood threshold' : '≥% shift') + '</span>'
        : '';
      html += '<tr>' +
        '<td>' + esc(k) + '</td>' +
        '<td>' + fmtMm(r.run_a_mm) + '</td>' +
        '<td>' + fmtMm(r.run_b_mm) + '</td>' +
        '<td class="' + cls + '">' + fmtDelta(d) + '</td>' +
        '<td>' + reason + '</td>' +
        '<td><span class="' + dotCls + '">' + (r.significant ? '●' : '') + '</span></td>' +
        '</tr>';
    });
    html += '</tbody></table></div>';
    if (!_showAll && sigKeys.length < keys.length) {
      html += '<div class="show-all-wrap"><button class="show-all-btn" onclick="showAll()">' +
        'Show all ' + keys.length + ' areas</button></div>';
    }
  }
  var tWrap = document.getElementById('tableWrap');
  if (tWrap) tWrap.innerHTML = html;
}
function showAll() { _showAll = true; renderTable(); }

/* ── Boot ────────────────────────────────────────────────────────────── */
function render(trendsData, metaData) {
  _data = trendsData;
  _meta = metaData;
  document.getElementById('loadingState').style.display = 'none';
  document.getElementById('mainContent').style.display  = '';
  document.getElementById('playerPanel').style.display  = 'flex';

  var metaEl = document.getElementById('headerMeta');
  if (trendsData.generated_at) {
    var d = new Date(trendsData.generated_at);
    var pad = function(n) { return n < 10 ? '0' + n : '' + n; };
    metaEl.textContent = 'Updated ' + pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ' GMT';
  }

  renderHeaderSelects();
  updateWindowUI();
  
  applyComparisonSelection().then(function() {
    try {
      initMap();
      drawPolys();
      updateRaster();
    } catch (e) {
      console.warn('Map unavailable:', e);
    }
  });
}

Promise.all([
  fetch('ukv_trends.json?_=' + Date.now()).then(function(r) { return r.json(); }),
  fetch('ukv_meta.json?_=' + Date.now()).then(function(r) { return r.json(); }).catch(function() { return null; })
]).then(function(results) {
  render(results[0], results[1]);
}).catch(function() {
  document.getElementById('loadingState').style.display = 'none';
  document.getElementById('errorState').style.display   = '';
});
"""

out_html = html[:script_start+8] + "\n" + new_script.strip() + "\n" + html[script_end:]
with open('ukv_trends.html', 'w', encoding='utf-8') as f:
    f.write(out_html)
print("Continuous time window patch applied successfully!")
