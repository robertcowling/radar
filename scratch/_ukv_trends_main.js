
// ── Config ────────────────────────────────────────────────────────────
var GEO_FILES = {
  catchments: { file: 'uk_catchments.geojson',          key: 'HA_NAME' },
  regions:    { file: 'uk_regions.geojson',              key: 'rgn19nm' },
  grid:       { file: 'uk_grid_20km.geojson',            key: 'name'    },
  summary:    { file: 'rainfallsummarypolygons.geojson', key: 'NAME'    }
};
var R2_CDN  = 'https://radar.floodforecast.co.uk';
var R2_BASE = 'https://pub-96089466ef9841fb90f34b6f89f0a090.r2.dev';
var IMG_BOUNDS = [[43.0,-26.0],[63.0,17.0]];

// Accumulation window sizes: key matches ukv_meta.json step keys
var WIN_SIZES = {
  'accum_1h':  { name: '1 hour',   hours: 1  },
  'accum_3h':  { name: '3 hours',  hours: 3  },
  'accum_6h':  { name: '6 hours',  hours: 6  },
  'accum_12h': { name: '12 hours', hours: 12 },
  'accum_24h': { name: '24 hours', hours: 24 }
};

var LAYER_LABEL = { catchments: 'Catchments', regions: 'Regions', grid: '20km grid cells', summary: 'Summary areas' };

var BIAS_BOUNDS = [-30,-15,-10,-5,-2,2,5,10,15,30];
var BIAS_COLORS = ['#67001f','#a50026','#d73027','#fdae61','#fdd9a0','#d4d4d4','#c6e2f0','#abd9e9','#74add1','#2166ac','#023858'];

var SCHEMES = {
  norm: { bounds:[0.5,1,2,5,10,20,40,80,160],   colors:['#e0f3ff','#81d4fa','#03a9f4','#01579b','#2e7d32','#fbc02d','#ef6c00','#c62828','#6a1b9a'] },
  high: { bounds:[2,5,10,25,50,100,150,200,350], colors:['#e0f3ff','#81d4fa','#03a9f4','#01579b','#2e7d32','#fbc02d','#ef6c00','#c62828','#6a1b9a'] },
  met:  { bounds:[0.03,1,5,10,20,40,60,80,100,120,140,160,180],
          colors:['#3A6CFF','#00FF00','#FFFF95','#FFD563','#FF9618','#E86100','#BA2000','#CC537D','#DB92DC','#FF02FF','#FFFFFF','#C8C8C8','#BFBF00'] }
};

// ── State ─────────────────────────────────────────────────────────────
var _data = null, _meta = null;
var _geoCache = {}, _tsCache = {}, _centroidCache = {};
var _layerKey = 'accum_6h', _layer = 'catchments', _scheme = 'met', _viewMode = 'grid';
var _sliderStep = null, _sliderSteps = [];
var _showAll = false;
var _syncing = false;

// Comparison model: one base run vs N reference runs (N = _compareMode: 1 or 4).
var _compareMode     = 1;
var _selectedRunLeft = null;   // base run_ts
var _refRuns         = [];     // reference run_ts list (length up to _compareMode)
var _baseTs          = null;   // base run area-ts
var _baseMaxOff      = 54;     // base non-null data extent (hours)
var _comps           = [];     // [{ runTs, ts, lagH, maxOff, data }]
var _panels          = [];     // [{ role:'base'|'ref'|'diff', compIndex, map, poly, label, raster, catch, legendEl }]

// ── Utilities ──────────────────────────────────────────────────────────
function esc(s) {
  return String(s).replace(/[&<>"]/g, function(c) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];
  });
}
function fmtMm(v)    { return v == null ? '—' : v.toFixed(1) + 'mm'; }
function fmtDelta(d) { return d == null ? '—' : (d >= 0 ? '+' : '') + d.toFixed(1) + 'mm'; }

function parseRunTs(ts) {
  return new Date(Date.UTC(+ts.slice(0,4), +ts.slice(4,6)-1, +ts.slice(6,8), +ts.slice(9,11), +ts.slice(11,13)));
}
function getRunLabel(ts) {
  if (_meta && _meta.runs) {
    var r = _meta.runs.find(function(x) { return x.run_ts === ts; });
    if (r) return r.run_label;
  }
  return ts;
}
function getWinLabel() {
  var ws = WIN_SIZES[_layerKey];
  return ws ? ws.name : _layerKey;
}
function getRunForecastHours(run_ts) {
  if (!_meta || !_meta.runs) return 54;
  var r = _meta.runs.find(function(x) { return x.run_ts === run_ts; });
  return (r && r.forecast_hours) || 54;
}
function getMetaStep(run_ts, offset_hours) {
  if (!_meta || !_meta.runs) return null;
  var r = _meta.runs.find(function(x) { return x.run_ts === run_ts; });
  if (!r) return null;
  return (r.steps || []).find(function(s) { return s.offset_hours === offset_hours; });
}
function fmtValidTime(offsetHours) {
  if (!_selectedRunLeft) return null;
  var dt = parseRunTs(_selectedRunLeft);
  dt = new Date(dt.getTime() + offsetHours * 3600000);
  var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  var p = function(n) { return n < 10 ? '0'+n : ''+n; };
  return { date: p(dt.getUTCDate()) + ' ' + months[dt.getUTCMonth()], time: p(dt.getUTCHours()) + ':00 UTC' };
}

// ── Colour ─────────────────────────────────────────────────────────────
function biasFillColor(mm) {
  if (mm == null) return null;
  for (var i = 0; i < BIAS_BOUNDS.length; i++) { if (mm < BIAS_BOUNDS[i]) return BIAS_COLORS[i]; }
  return BIAS_COLORS[BIAS_BOUNDS.length];
}
function bucketColor(v, scheme) {
  if (v == null) return null;
  var s = SCHEMES[scheme || _scheme];
  if (v < s.bounds[0]) return null;
  for (var i = s.bounds.length - 1; i >= 0; i--) { if (v >= s.bounds[i]) return s.colors[i]; }
  return null;
}

// ── Data access ─────────────────────────────────────────────────────────
// Table / insights summarise the first (most-recent) reference comparison.
function currentComp() {
  var c0 = _comps[0];
  return { run_a_label: getRunLabel(_selectedRunLeft), run_b_label: c0 ? getRunLabel(c0.runTs) : '' };
}
function compRows(i) {
  var c = _comps[i];
  return (c && c.data && c.data[_layer]) || {};
}
function currentRows() { return compRows(0); }
// Base value is shared across comparisons; read it from whichever comp has it.
function baseValue(name) {
  for (var i = 0; i < _comps.length; i++) {
    var r = compRows(i)[name];
    if (r && r.run_a_mm != null) return r.run_a_mm;
  }
  return null;
}
function valueForPanel(name, panel) {
  if (panel.role === 'base') return baseValue(name);
  var r = compRows(panel.compIndex)[name];
  if (!r) return null;
  return panel.role === 'diff' ? r.delta_mm : r.run_b_mm;
}

// ── Modal helpers ────────────────────────────────────────────────────────
function openInsightsModal()  { document.getElementById('insightsModalBackdrop').classList.add('open'); }
function closeInsightsModal() { document.getElementById('insightsModalBackdrop').classList.remove('open'); }
function openTableModal()     { document.getElementById('tableModalBackdrop').classList.add('open'); }
function closeTableModal()    { document.getElementById('tableModalBackdrop').classList.remove('open'); }
document.addEventListener('keydown', function(e) { if (e.key==='Escape') { closeInsightsModal(); closeTableModal(); } });

// ── Fetch helpers ────────────────────────────────────────────────────────
async function loadRunAreaTs(run_ts) {
  if (_tsCache[run_ts]) return _tsCache[run_ts];
  try {
    var r = await fetch(R2_CDN + '/ukv_area_ts/' + run_ts + '.json');
    if (!r.ok) return null;
    var d = await r.json();
    _tsCache[run_ts] = d;
    return d;
  } catch(e) { return null; }
}
function loadGeo(layer) {
  if (_geoCache[layer]) return Promise.resolve(_geoCache[layer]);
  return fetch(GEO_FILES[layer].file)
    .then(function(r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(function(g) { _geoCache[layer] = g; return g; });
}

// ── Computation ──────────────────────────────────────────────────────────
// Sum hourly increments over the forecast window (startH, endH] in *offset hours*.
// Values are mapped through step_hours[] (never by raw array index) so non-contiguous
// or capped time series stay correct. Returns null if the window isn't fully covered.
function sumWindow(ts, lyr, name, startH, endH) {
  if (!ts) return null;
  var sh    = ts.step_hours || [];
  var areas = ts[lyr]; if (!areas) return null;
  var arr   = areas[name]; if (!arr) return null;
  var sum = 0, count = 0;
  for (var i = 0; i < sh.length; i++) {
    if (sh[i] > startH && sh[i] <= endH) {
      var v = arr[i];
      if (v == null) return null;   // a missing hour means the window is incomplete
      sum += v; count++;
    }
  }
  if (count < (endH - startH)) return null;  // not every hour in the window is present
  return sum;
}

// Compare base (A) and reference (B) over the same valid-time window.
// Base window  = (T - W, T]  in A's forecast frame.
// Ref  window  = same valid time → (T + lag - W, T + lag]  in B's frame (lag = dtA - dtB).
function computeLayerWindow(tsA, tsB, T, W, lag_h, lyr, rules) {
  var floor  = (lyr === 'grid') ? (rules.grid_floor_mm || 5) : (rules.catchment_floor_mm || 2);
  var areasA = (tsA && tsA[lyr]) || {}, areasB = (tsB && tsB[lyr]) || {};
  var keys   = Object.keys(areasA).length ? Object.keys(areasA) : Object.keys(areasB);
  var result = {};

  for (var i = 0; i < keys.length; i++) {
    var name = keys[i];
    var sumA = sumWindow(tsA, lyr, name, T - W,         T);
    var sumB = sumWindow(tsB, lyr, name, T + lag_h - W, T + lag_h);
    if (sumA === null && sumB === null) continue;

    var va    = sumA !== null ? Math.round(sumA * 100) / 100 : null;
    var vb    = sumB !== null ? Math.round(sumB * 100) / 100 : null;
    var delta = (va !== null && vb !== null) ? Math.round((va - vb) * 100) / 100 : null;
    var sig = false, reason = null;

    if (delta !== null && Math.abs(delta) >= floor) {
      var tlist = rules.thresholds_mm || [10,25,50,100];
      for (var j = 0; j < tlist.length; j++) {
        if ((va >= tlist[j] && vb < tlist[j]) || (va < tlist[j] && vb >= tlist[j])) {
          sig = true; reason = 'threshold'; break;
        }
      }
      if (!sig && vb > 0) {
        var pc = (rules.relative_change_pct || 20) / 100;
        if (Math.abs(delta) / vb >= pc) { sig = true; reason = 'relative'; }
      }
    }
    result[name] = { run_a_mm: va, run_b_mm: vb, delta_mm: delta, significant: sig, reason: reason };
  }
  return result;
}

// Largest offset hour for which a run has actually computed data. step_hours may
// claim 54h while the values are still null past (say) 48h — a dry area reads 0.0,
// never null, so a null genuinely means "not produced for this timestep".
function dataExtent(ts) {
  if (!ts) return 0;
  var sh = ts.step_hours || [];
  var maxIdx = -1;
  ['catchments', 'regions', 'grid', 'summary'].forEach(function(lyr) {
    var areas = ts[lyr]; if (!areas) return;
    var names = Object.keys(areas);
    for (var n = 0; n < names.length; n++) {
      var arr = areas[names[n]];
      for (var i = arr.length - 1; i > maxIdx; i--) {
        if (arr[i] != null) { if (i > maxIdx) maxIdx = i; break; }
      }
    }
  });
  return maxIdx >= 0 ? (sh[maxIdx] || 0) : 0;
}

function computeCompData() {
  var W     = WIN_SIZES[_layerKey] ? WIN_SIZES[_layerKey].hours : 6;
  var rules = (_data && _data.significance_rules) || {};
  _comps.forEach(function(c) {
    c.data = {};
    if (!_baseTs || !c.ts || _sliderStep === null) return;
    ['catchments', 'regions', 'grid', 'summary'].forEach(function(lyr) {
      c.data[lyr] = computeLayerWindow(_baseTs, c.ts, _sliderStep, W, c.lagH, lyr, rules);
    });
  });
}

// ── Slider ──────────────────────────────────────────────────────────────
// The single slider position T (base forecast frame) must yield a valid window for
// EVERY active comparison, so the range is the intersection across all references.
function buildSliderRange() {
  var W = WIN_SIZES[_layerKey] ? WIN_SIZES[_layerKey].hours : 6;

  // Base constrains W <= T <= baseMaxOff; each reference adds W-lag <= T <= maxOff-lag.
  var tMin = W, tMax = _baseMaxOff;
  _comps.forEach(function(c) {
    tMin = Math.max(tMin, W - c.lagH);
    tMax = Math.min(tMax, c.maxOff - c.lagH);
  });

  function rasterAt(runTs, off) { var s = getMetaStep(runTs, off); return !!(s && s[_layerKey]); }

  // Step hourly; keep T only when the base AND every reference have the product at the
  // matching valid time (base@T, ref_i@T+lag_i). Guarantees a real comparison everywhere.
  _sliderSteps = [];
  if (_comps.length) {
    for (var t = tMin; t <= tMax; t++) {
      if (!rasterAt(_selectedRunLeft, t)) continue;
      var ok = _comps.every(function(c) { return rasterAt(c.runTs, t + c.lagH); });
      if (ok) _sliderSteps.push(t);
    }
  }

  var slider = document.getElementById('timeSlider');
  if (!slider) return;

  slider.min  = 0;
  slider.max  = Math.max(0, _sliderSteps.length - 1);
  slider.step = 1;

  var noMsg = document.getElementById('noOverlapMsg');

  if (!_sliderSteps.length) {
    _sliderStep      = null;
    slider.value     = 0;
    slider.disabled  = true;
    if (noMsg) noMsg.style.display = '';
    updateSliderLabel();
    renderTimelineTicks();
    return;
  }

  slider.disabled  = false;
  if (noMsg) noMsg.style.display = 'none';

  var prevIdx = _sliderSteps.indexOf(_sliderStep);
  if (prevIdx < 0) {
    var midIdx = Math.floor(_sliderSteps.length / 2);
    _sliderStep  = _sliderSteps[midIdx];
    slider.value = midIdx;
  } else {
    slider.value = prevIdx;
  }
  updateSliderLabel();
  renderTimelineTicks();
}

function setSliderStep(idx) {
  _sliderStep = (_sliderSteps[idx] !== undefined) ? _sliderSteps[idx] : null;
  updateSliderLabel();
  computeCompData();
  refreshPolyStyles();  // also rebuilds labels
  updateRaster();
  renderLegend();
  renderTable();
  renderInsights(buildInsights());
}

function updateSliderLabel() {
  var dateLine   = document.getElementById('playerDateLine');
  var timeLine   = document.getElementById('playerTimeLine');
  var offsetLine = document.getElementById('playerOffsetLine');
  var empty = function() {
    if (dateLine)   dateLine.textContent   = '—';
    if (timeLine)   timeLine.textContent   = '—';
    if (offsetLine) offsetLine.textContent = '';
  };
  if (_sliderStep === null || !_selectedRunLeft) { empty(); return; }
  var vt = fmtValidTime(_sliderStep);
  if (!vt) { empty(); return; }
  if (dateLine)   dateLine.textContent   = vt.date;
  if (timeLine)   timeLine.textContent   = vt.time;
  if (offsetLine) offsetLine.textContent = 'T+' + _sliderStep + 'h';
}

function renderTimelineTicks() {
  var container = document.getElementById('timelineTicks');
  if (!container) return;
  container.innerHTML = '';
  if (!_sliderSteps.length || !_selectedRunLeft) return;

  var tMin = _sliderSteps[0];
  var tMax = _sliderSteps[_sliderSteps.length - 1];
  var range = tMax - tMin;
  if (range <= 0) return;

  var baseStart = parseRunTs(_selectedRunLeft);
  var MONTHS    = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  var pad       = function(n) { return n < 10 ? '0' + n : '' + n; };

  // Pick a tick interval that gives 4-8 ticks
  var intervals = [1,2,3,6,12,24,48];
  var tickStep  = intervals.find(function(iv) { return range / iv <= 8 && range / iv >= 2; }) || 6;

  var firstTick = Math.ceil(tMin / tickStep) * tickStep;
  for (var h = firstTick; h <= tMax; h += tickStep) {
    var pct = ((h - tMin) / range * 100).toFixed(2);
    var dt  = new Date(baseStart.getTime() + h * 3600000);
    var hh  = dt.getUTCHours();
    var isMidnight = (hh === 0);

    var el       = document.createElement('span');
    el.className = 'tick-mark' + (isMidnight ? ' tick-date' : '');
    el.style.left = pct + '%';
    el.textContent = isMidnight
      ? pad(dt.getUTCDate()) + ' ' + MONTHS[dt.getUTCMonth()]
      : pad(hh) + ':00';
    container.appendChild(el);
  }
}

// ── Run selectors ─────────────────────────────────────────────────────────
function overlappingRuns() {
  var allRuns = (_meta && _meta.runs) || [];
  if (!_selectedRunLeft) return [];
  var dtLeft = parseRunTs(_selectedRunLeft);
  var fhA    = getRunForecastHours(_selectedRunLeft);
  // Shared valid-time window, measured in the base (A) forecast frame:
  //   start offset = max(0, -lag),  end offset = min(fhA, fhB - lag),  lag = dtA - dtB.
  // Keep runs with at least 1h of overlap.
  return allRuns.filter(function(r) {
    if (r.run_ts === _selectedRunLeft) return false;
    var lag     = Math.round((dtLeft - parseRunTs(r.run_ts)) / 3600000);
    var fhB     = getRunForecastHours(r.run_ts);
    var overlap = Math.min(fhA, fhB - lag) - Math.max(0, -lag);
    return overlap >= 1;
  });
}

// Default comparison runs, by lag from the base: previous run, then ~12h before,
// then ~24h before (picking the closest available overlapping run to each target).
function pickRunByLag(avail, dtBase, targetLag, used) {
  var best = null, bestDiff = Infinity;
  avail.forEach(function(r) {
    if (used.indexOf(r.run_ts) >= 0) return;
    var lag  = Math.round((dtBase - parseRunTs(r.run_ts)) / 3600000);
    var diff = Math.abs(lag - targetLag);
    if (diff < bestDiff) { bestDiff = diff; best = r.run_ts; }
  });
  return best;
}

function defaultRefs() {
  var avail = overlappingRuns();
  if (!_selectedRunLeft || !avail.length) return [];
  var dtBase = parseRunTs(_selectedRunLeft);
  var lagOf  = function(ts) { return Math.round((dtBase - parseRunTs(ts)) / 3600000); };
  var used = [], out = [];

  // 1st comparison = the immediately preceding run (smallest positive lag).
  var prev = avail.slice().sort(function(a, b) { return lagOf(a.run_ts) - lagOf(b.run_ts); })[0];
  if (prev) { out.push(prev.run_ts); used.push(prev.run_ts); }

  // Then ~12h and ~24h before the base.
  [12, 24].forEach(function(target) {
    if (out.length >= _compareMode) return;
    var pick = pickRunByLag(avail, dtBase, target, used);
    if (pick) { out.push(pick); used.push(pick); }
  });

  // Top up from remaining overlapping runs if more slots are needed.
  for (var i = 0; i < avail.length && out.length < _compareMode; i++) {
    if (used.indexOf(avail[i].run_ts) < 0) { out.push(avail[i].run_ts); used.push(avail[i].run_ts); }
  }
  return out.slice(0, _compareMode);
}

// Keep references valid against the current base; preserve user picks, fill the rest
// from the lag-based defaults (previous / −12h / −24h).
function reconcileRefs() {
  var avail    = overlappingRuns();
  var availSet = avail.map(function(r) { return r.run_ts; });
  var kept     = _refRuns.filter(function(rt) { return availSet.indexOf(rt) >= 0; });
  kept = kept.filter(function(rt, i) { return kept.indexOf(rt) === i; });   // dedupe
  if (kept.length < _compareMode) {
    var defs = defaultRefs();
    for (var i = 0; i < defs.length && kept.length < _compareMode; i++) {
      if (kept.indexOf(defs[i]) < 0) kept.push(defs[i]);
    }
    for (var j = 0; j < avail.length && kept.length < _compareMode; j++) {
      if (kept.indexOf(avail[j].run_ts) < 0) kept.push(avail[j].run_ts);
    }
  }
  _refRuns = kept.slice(0, _compareMode);
}

// Compact run label for the in-panel selectors, e.g. "28 Jun 03:00".
function shortRunLabel(ts) {
  var m = ts.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})Z$/);
  if (!m) return getRunLabel(ts);
  var mon = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  return (+m[3]) + ' ' + mon[+m[2]-1] + ' ' + m[4] + ':' + m[5];
}

function populateSelects() {
  var allRuns = (_meta && _meta.runs) || [];
  var base = document.getElementById('selBase');
  if (base) base.innerHTML = allRuns.map(function(r) {
    return '<option value="' + esc(r.run_ts) + '"' + (r.run_ts === _selectedRunLeft ? ' selected' : '') + '>' + esc(shortRunLabel(r.run_ts)) + '</option>';
  }).join('');

  var avail  = overlappingRuns();
  var dtLeft = _selectedRunLeft ? parseRunTs(_selectedRunLeft) : null;
  for (var i = 0; i < _compareMode; i++) {
    var sel = document.getElementById('selRef' + i);
    if (!sel) continue;
    var cur = _refRuns[i];
    sel.innerHTML = avail.map(function(r) {
      var lag    = dtLeft ? Math.round((dtLeft - parseRunTs(r.run_ts)) / 3600000) : 0;
      var lagStr = (lag > 0 ? '−' : '+') + Math.abs(lag) + 'h';
      return '<option value="' + esc(r.run_ts) + '"' + (r.run_ts === cur ? ' selected' : '') + '>' + esc(shortRunLabel(r.run_ts)) + ' (' + lagStr + ')</option>';
    }).join('');
  }
}

function onBaseChange(v) { _selectedRunLeft = v; _refRuns = []; applyAll(); }
function onRefChange(i, v) { _refRuns[i] = v; applyAll(); }

function setCompareMode(n) {
  if (_compareMode === n) return;
  _compareMode = n;
  document.getElementById('btnCmp1').classList.toggle('seg-active', n === 1);
  document.getElementById('btnCmp3').classList.toggle('seg-active', n === 3);
  reconcileRefs();
  buildMaps();      // rebuild the panel grid for the new column count
  applyAll();
}

async function applyAll() {
  _showAll = false;
  var allRuns = (_meta && _meta.runs) || [];
  if (!_selectedRunLeft) _selectedRunLeft = (_data && _data.latest_run) || (allRuns[0] && allRuns[0].run_ts);

  reconcileRefs();
  populateSelects();

  if (!_selectedRunLeft || !_refRuns.length) {
    document.getElementById('tableWrap').innerHTML = '<p class="empty-msg">Select a base and at least one reference run to compare.</p>';
    return;
  }

  var tsArr = await Promise.all([loadRunAreaTs(_selectedRunLeft)].concat(_refRuns.map(loadRunAreaTs)));
  _baseTs     = tsArr[0];
  _baseMaxOff = dataExtent(_baseTs) || getRunForecastHours(_selectedRunLeft);
  var dtBase  = parseRunTs(_selectedRunLeft);

  _comps = _refRuns.map(function(rt, i) {
    var ts = tsArr[i + 1];
    return {
      runTs:  rt,
      ts:     ts,
      lagH:   Math.round((dtBase - parseRunTs(rt)) / 3600000),
      maxOff: dataExtent(ts) || getRunForecastHours(rt),
      data:   {}
    };
  });

  buildSliderRange();
  computeCompData();

  drawPolys();
  updateCatchOverlay();
  renderLegend();
  updateRaster();
  renderTable();
  renderRules();
  renderInsights(buildInsights());
}

// ── Insights & Rules ──────────────────────────────────────────────────────
function buildInsights() {
  var c0   = _comps[0];
  var rows = (c0 && c0.data && c0.data['catchments']) || {};
  var sig  = Object.keys(rows).filter(function(k) { return rows[k].significant; });
  if (!sig.length) return ['No significant catchment-level changes detected for this window.'];
  var wetter = sig.filter(function(k) { return rows[k].delta_mm > 0; }).length;
  var drier  = sig.filter(function(k) { return rows[k].delta_mm < 0; }).length;
  return [sig.length + ' catchments show significant changes — ' + wetter + ' wetter, ' + drier + ' drier.'];
}

function renderInsights(insights) {
  var el = document.getElementById('insightsBody');
  if (!el) return;
  if (!insights || !insights.length) {
    el.innerHTML = '<p style="font-size:13px;color:#64748b;">No significant run-to-run changes in this window.</p>';
    return;
  }
  el.innerHTML = insights.map(function(t) {
    return '<div class="insight-row"><span class="insight-dot">•</span><span>' + esc(t) + '</span></div>';
  }).join('');
}

function renderRules() {
  var rules = (_data && _data.significance_rules) || {};
  var cf = rules.catchment_floor_mm != null ? rules.catchment_floor_mm : 2;
  var gf = rules.grid_floor_mm      != null ? rules.grid_floor_mm      : 5;
  var th = (rules.thresholds_mm || [10,25,50,100]).join(', ');
  var pc = rules.relative_change_pct != null ? rules.relative_change_pct : 20;
  var el = document.getElementById('rulesBody');
  if (!el) return;
  el.innerHTML = '<div class="rules-note">Areas are compared at the <b>same valid time</b> across runs. A change is flagged as significant when <b>both</b> hold:<ul>' +
    '<li><b>Noise floor cleared</b> — change exceeds <code>' + cf + 'mm</code> for catchments/regions or <code>' + gf + 'mm</code> for grid cells.</li>' +
    '<li><b>Operationally relevant</b> — total crosses a flood threshold (<code>' + th + 'mm</code>), or the prior total shifts by more than <code>' + pc + '%</code>.</li>' +
    '</ul>Significant rows are marked: <span style="color:#b91c1c;font-weight:700;">●</span> base wetter, <span style="color:#1565c0;font-weight:700;">●</span> base drier.</div>';
}

// ── Map grid construction ───────────────────────────────────────────────────
// Layout: [ base ] then for each reference: [ reference ][ difference ].
function panelSpecs() {
  var specs = [{ role: 'base' }];
  for (var i = 0; i < _compareMode; i++) {
    specs.push({ role: 'ref',  compIndex: i });
    specs.push({ role: 'diff', compIndex: i });
  }
  return specs;
}

function buildMaps() {
  _panels.forEach(function(p) { try { p.map.remove(); } catch(e) {} });
  _panels = [];

  var row = document.getElementById('mapsRow');
  row.innerHTML = '';

  var multi = _compareMode > 1;
  // Single-run: simple flex row [base | ref | diff]. Multi-run: base spans the left
  // third (all rows); reference column + difference column stack N rows on the right.
  row.className = 'maps-row' + (multi ? ' mode-multi' : '');
  row.style.gridTemplateRows = multi ? 'repeat(' + _compareMode + ', 1fr)' : '';

  var CENTER   = [54.5, -3.5], ZOOM = 5;
  var TILE_URL = 'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png';
  var TILE_OPT = { maxZoom: 12, attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> © <a href="https://carto.com/">CARTO</a>' };

  panelSpecs().forEach(function(spec, idx) {
    var wrap   = document.createElement('div'); wrap.className = 'map-wrap';
    var pane   = document.createElement('div'); pane.className = 'map-pane'; pane.id = 'map_' + idx;
    var legend = document.createElement('div'); legend.className = 'map-cbar';

    // Grid placement: col1 = base (spans rows), col2 = references, col3 = differences.
    if (multi) {
      if (spec.role === 'base')      { wrap.style.gridColumn = '1'; wrap.style.gridRow = '1 / span ' + _compareMode; }
      else if (spec.role === 'ref')  { wrap.style.gridColumn = '2'; wrap.style.gridRow = (spec.compIndex + 1); }
      else                           { wrap.style.gridColumn = '3'; wrap.style.gridRow = (spec.compIndex + 1); }
    }

    // In-panel control overlay (run selector for base/ref, label for diff)
    var ctrl = document.createElement('div');
    if (spec.role === 'diff') {
      ctrl.className = 'panel-ctrl diff-ctrl';
      ctrl.innerHTML = '<span class="role">Difference</span>';
    } else {
      ctrl.className = 'panel-ctrl';
      var roleTxt = spec.role === 'base' ? 'Base' : 'Ref ' + (spec.compIndex + 1);
      var selId   = spec.role === 'base' ? 'selBase' : 'selRef' + spec.compIndex;
      ctrl.innerHTML = '<span class="role">' + roleTxt + '</span><select id="' + selId + '" class="panel-select"></select>';
    }

    wrap.appendChild(pane); wrap.appendChild(legend); wrap.appendChild(ctrl);
    row.appendChild(wrap);

    var map = L.map(pane, { zoomControl: false }).setView(CENTER, ZOOM);
    L.tileLayer(TILE_URL, TILE_OPT).addTo(map);

    _panels.push({ role: spec.role, compIndex: spec.compIndex, map: map, legendEl: legend,
                   poly: null, label: null, raster: null, catch: null });
  });

  // Wire up selectors
  var base = document.getElementById('selBase');
  if (base) base.addEventListener('change', function() { onBaseChange(base.value); });
  for (var i = 0; i < _compareMode; i++) {
    (function(ci) {
      var sel = document.getElementById('selRef' + ci);
      if (sel) sel.addEventListener('change', function() { onRefChange(ci, sel.value); });
    })(i);
  }

  // Synchronise pan/zoom across all panels
  _panels.forEach(function(p) {
    p.map.on('moveend', function() {
      if (_syncing) return; _syncing = true;
      var c = p.map.getCenter(), z = p.map.getZoom();
      _panels.forEach(function(q) { if (q !== p) q.map.setView(c, z, { animate: false }); });
      _syncing = false;
    });
  });

  populateSelects();
  setTimeout(function() { _panels.forEach(function(p) { p.map.invalidateSize(); }); }, 60);
}

function styleFeature(feature, panel) {
  var info   = GEO_FILES[_layer];
  var name   = feature.properties[info.key];
  var v      = valueForPanel(name, panel);
  var isDiff = panel.role === 'diff';

  // Grid view: base & reference show rasters → polys are thin outlines only.
  if (_viewMode === 'grid' && !isDiff) {
    return { color: '#475569', weight: 0.5, fillColor: '#000', fillOpacity: 0 };
  }
  var color = isDiff ? biasFillColor(v) : bucketColor(v);
  if (isDiff && v != null && !color) {
    return { color: '#9ca3af', weight: 0.6, fillColor: '#d4d4d4', fillOpacity: 0.6 };  // within ±2
  }
  if (!color) {
    return { color: '#b0b8c1', weight: 0.5, fillColor: '#e8edf2', fillOpacity: 0.45 };  // dry but present
  }
  return { color: '#334155', weight: 0.6, fillColor: color, fillOpacity: 0.72 };
}

function tooltipContent(name, compIndex) {
  var c    = _comps[compIndex];
  var rows = (c && c.data && c.data[_layer]) || {};
  var r    = rows[name];
  if (!r) return '<div class="poly-tip"><b>' + esc(name) + '</b><br>No data for this window.</div>';
  var d   = r.delta_mm;
  var cls = d > 0 ? 'pt-pos' : (d < 0 ? 'pt-neg' : '');
  return '<div class="poly-tip"><b>' + esc(name) + '</b><br>' +
    esc(getRunLabel(_selectedRunLeft))        + ': ' + fmtMm(r.run_a_mm) + '<br>' +
    esc(c ? getRunLabel(c.runTs) : 'Reference') + ': ' + fmtMm(r.run_b_mm) + '<br>' +
    'Change: <span class="' + cls + '">' + fmtDelta(d) + '</span>' +
    (r.significant ? ' ●' : '') + '</div>';
}

// Cache area centroids per layer (computed once) so labelling 9 panels stays cheap.
function centroidsFor(geo, info) {
  if (_centroidCache[_layer]) return _centroidCache[_layer];
  var m = {};
  geo.features.forEach(function(f) {
    var name = f.properties[info.key];
    var b = L.geoJSON(f).getBounds();
    if (b.isValid()) m[name] = b.getCenter();
  });
  _centroidCache[_layer] = m;
  return m;
}

function buildAllLabels(geo, info) {
  var cents = centroidsFor(geo, info);
  _panels.forEach(function(p) {
    if (p.label) { p.map.removeLayer(p.label); p.label = null; }
    var show = (p.role === 'diff') || (_viewMode !== 'grid');   // diff always; base/ref only in area view
    if (!show) return;
    var group  = L.layerGroup();
    var isBias = p.role === 'diff';
    geo.features.forEach(function(f) {
      var name = f.properties[info.key];
      var v = valueForPanel(name, p);
      if (v == null) return;
      var centroid = cents[name];
      if (!centroid) return;
      var txt = isBias ? (v >= 0 ? '+' : '') + v.toFixed(1) : v.toFixed(1);
      var cls = 'area-value-label' + (isBias && v > 0.5 ? ' label-pos' : isBias && v < -0.5 ? ' label-neg' : '');
      L.marker(centroid, {
        icon: L.divIcon({ className: cls, html: txt, iconSize: [44, 18], iconAnchor: [22, 9] }),
        interactive: false
      }).addTo(group);
    });
    p.label = group; group.addTo(p.map);
  });
}

function drawPolys() {
  if (!_panels.length) return;
  loadGeo(_layer).then(function(geo) {
    var info = GEO_FILES[_layer];
    _panels.forEach(function(p) {
      if (p.poly) { p.map.removeLayer(p.poly); p.poly = null; }
      var ci = p.role === 'base' ? 0 : p.compIndex;
      p.poly = L.geoJSON(geo, {
        style: function(f) { return styleFeature(f, p); },
        onEachFeature: function(feature, lyr) {
          var name = feature.properties[info.key];
          lyr.bindTooltip(function() { return tooltipContent(name, ci); }, { sticky: true, className: 'poly-tooltip' });
        }
      }).addTo(p.map);
    });
    buildAllLabels(geo, info);
    try {
      var b = _panels[0].poly.getBounds();
      if (b.isValid()) _panels.forEach(function(p) { p.map.fitBounds(b, { padding: [12,12], maxZoom: 8 }); });
    } catch(e) {}
  }).catch(function() {});
}

function refreshPolyStyles() {
  _panels.forEach(function(p) {
    if (p.poly) p.poly.setStyle(function(f) { return styleFeature(f, p); });
  });
  var geo = _geoCache[_layer]; if (!geo) return;
  buildAllLabels(geo, GEO_FILES[_layer]);
}

// ── Raster overlays ────────────────────────────────────────────────────────
// Grid view: base panel shows the base image; each reference panel shows that run's
// image at the SAME valid time (offset T + lag). Diff panels stay polygon choropleths.
function updateRaster() {
  _panels.forEach(function(p) {
    if (p.raster) { p.map.removeLayer(p.raster); p.raster = null; }
  });
  if (_viewMode !== 'grid' || _sliderStep === null || !_selectedRunLeft) return;

  _panels.forEach(function(p) {
    if (p.role === 'diff') return;
    var runTs, off;
    if (p.role === 'base') { runTs = _selectedRunLeft; off = _sliderStep; }
    else {
      var c = _comps[p.compIndex]; if (!c) return;
      runTs = c.runTs; off = _sliderStep + c.lagH;
    }
    var step = getMetaStep(runTs, off); if (!step) return;
    var entry = step[_layerKey]; if (!entry) return;
    var url = typeof entry === 'string' ? entry : (entry[_scheme] || entry.norm); if (!url) return;
    url = url.replace(R2_BASE, R2_CDN);
    p.raster = L.imageOverlay(url, IMG_BOUNDS, { opacity: 0.85, interactive: false }).addTo(p.map);
  });
}

// ── Legend ─────────────────────────────────────────────────────────────────
function accumCbarHtml() {
  var s = SCHEMES[_scheme];
  var blocks = s.colors.map(function(c) { return '<div class="cbar-block" style="background:' + c + '"></div>'; }).join('');
  var n = s.colors.length;
  var labels = s.bounds.map(function(v, i) {
    var pct = ((i + 0.5) / n * 100).toFixed(1);
    var str = v < 1 ? ('.' + String(v).split('.')[1]) : Math.round(v);
    return '<span class="cbar-label" style="left:' + pct + '%">' + str + '</span>';
  }).join('');
  return '<div class="cbar">' + blocks + '</div><div class="cbar-labels">' + labels + '</div>';
}

function diffCbarHtml() {
  var blocks = BIAS_COLORS.map(function(c) { return '<div class="cbar-block" style="background:' + c + '"></div>'; }).join('');
  var n = BIAS_COLORS.length;
  var labels = BIAS_BOUNDS.map(function(v, i) {
    var pct = ((i + 1) / n * 100).toFixed(1);
    var txt = v === -2 ? '−2' : v === 2 ? '+2' : (v > 0 ? '+' : '') + Math.round(v);
    return '<span class="cbar-label" style="left:' + pct + '%">' + txt + '</span>';
  }).join('');
  return '<p>Difference (base − reference)</p>' +
    '<div class="cbar">' + blocks + '</div>' +
    '<div class="cbar-labels">' + labels + '</div>' +
    '<div class="bias-arrows"><span style="color:#1565c0;font-weight:600;">◄ Base drier</span><span style="color:#b91c1c;font-weight:600;">Base wetter ►</span></div>';
}

function renderLegend() {
  // One colour key per column (base, references share the accumulation scale; the
  // difference column has its own). Show it on the top panel of each column only.
  _panels.forEach(function(p) {
    if (!p.legendEl) return;
    var showKey = (p.role === 'base') || (p.compIndex === 0);
    p.legendEl.style.display = showKey ? '' : 'none';
    if (showKey) p.legendEl.innerHTML = (p.role === 'diff') ? diffCbarHtml() : accumCbarHtml();
  });
}

// ── Catchment reference overlay (shown on all panels when the Grid 20km layer is active) ──
function updateCatchOverlay() {
  _panels.forEach(function(p) { if (p.catch) { p.map.removeLayer(p.catch); p.catch = null; } });
  if (_layer !== 'grid') return;
  loadGeo('catchments').then(function(geo) {
    _panels.forEach(function(p) {
      p.catch = L.geoJSON(geo, { style: { color: '#64748b', weight: 1, fillOpacity: 0, interactive: false } }).addTo(p.map);
    });
  }).catch(function() {});
}

// ── Control handlers ────────────────────────────────────────────────────────
function setLayerKey(key) {
  _layerKey = key;
  buildSliderRange();   // also calls updateSliderLabel + renderTimelineTicks
  computeCompData();
  refreshPolyStyles();
  updateRaster();
  renderLegend();
  renderTable();
  renderInsights(buildInsights());
}

function setLayer(lyr) {
  _layer = lyr;
  drawPolys();
  updateCatchOverlay();
  renderLegend();
  renderTable();
}

function setViewMode(mode) {
  _viewMode = mode;
  document.getElementById('btnGrid').classList.toggle('seg-active', mode === 'grid');
  document.getElementById('btnArea').classList.toggle('seg-active', mode === 'area');
  refreshPolyStyles();  // also rebuilds labels
  updateRaster();
}

function setScheme(s) {
  _scheme = s;
  refreshPolyStyles();
  renderLegend();
  updateRaster();
}

// ── Data Table ──────────────────────────────────────────────────────────────
function renderTable() {
  var comp = currentComp();
  var rows = currentRows();
  var keys = Object.keys(rows);

  var wLbl = document.getElementById('tblWinLabel');
  var lLbl = document.getElementById('tblLayerLabel');
  if (wLbl) wLbl.textContent = getWinLabel();
  if (lLbl) lLbl.textContent = LAYER_LABEL[_layer] || _layer;

  keys.sort(function(a, b) {
    var sa = rows[a].significant ? 1 : 0, sb = rows[b].significant ? 1 : 0;
    if (sa !== sb) return sb - sa;
    return Math.abs(rows[b].delta_mm || 0) - Math.abs(rows[a].delta_mm || 0);
  });

  var sigKeys = keys.filter(function(k) { return rows[k].significant; });
  var display = (_showAll || !sigKeys.length) ? keys : sigKeys;

  var tw = document.getElementById('tableWrap');
  if (!tw) return;

  if (!display.length) {
    tw.innerHTML = '<p class="empty-msg">No data for this window / layer combination.</p>';
    return;
  }

  var html = '<div style="overflow-x:auto;"><table class="data-table"><thead><tr>' +
    '<th>Area</th><th>' + esc(comp.run_a_label||'Base') + '</th>' +
    '<th>' + esc(comp.run_b_label||'Reference') + '</th>' +
    '<th>Change</th><th>Flagged</th><th></th></tr></thead><tbody>';

  display.forEach(function(k) {
    var r   = rows[k], d = r.delta_mm;
    var cls = d > 0 ? 'delta-pos' : (d < 0 ? 'delta-neg' : '');
    var dot = r.significant ? '<span class="sig-dot ' + (d > 0 ? 'wetter' : 'drier') + '">●</span>' : '';
    var pill = (r.significant && r.reason)
      ? '<span class="reason-pill">' + (r.reason === 'threshold' ? 'Flood threshold' : '≥% shift') + '</span>' : '';
    html += '<tr><td>' + esc(k) + '</td><td>' + fmtMm(r.run_a_mm) + '</td><td>' + fmtMm(r.run_b_mm) + '</td>' +
      '<td class="' + cls + '">' + fmtDelta(d) + '</td><td>' + pill + '</td><td>' + dot + '</td></tr>';
  });

  html += '</tbody></table></div>';
  if (!_showAll && sigKeys.length < keys.length) {
    html += '<div class="show-all-wrap"><button class="show-all-btn" onclick="_showAll=true;renderTable()">Show all ' + keys.length + ' areas</button></div>';
  }
  tw.innerHTML = html;
}

// ── Boot ────────────────────────────────────────────────────────────────────
function showMain() {
  document.getElementById('loadingState').style.display = 'none';
  document.getElementById('mapsRow').style.display      = '';   // let the .maps-row class drive flex/grid
  document.getElementById('playerPanel').style.display  = 'flex';
}

function init(trendsData, metaData) {
  _data = trendsData;
  _meta = metaData;

  var metaEl = document.getElementById('headerMeta');
  if (trendsData && trendsData.generated_at) {
    var d = new Date(trendsData.generated_at);
    var p = function(n) { return n < 10 ? '0' + n : '' + n; };
    metaEl.textContent = 'Updated ' + p(d.getUTCHours()) + ':' + p(d.getUTCMinutes()) + ' UTC';
  }

  var allRuns = (_meta && _meta.runs) || [];
  _selectedRunLeft = (_data && _data.latest_run) || (allRuns[0] && allRuns[0].run_ts);
  reconcileRefs();

  showMain();
  buildMaps();
  renderRules();
  applyAll();
}

window.addEventListener('resize', function() {
  _panels.forEach(function(p) { p.map.invalidateSize(); });
});

Promise.all([
  fetch('ukv_trends.json?_=' + Date.now()).then(function(r) { return r.json(); }).catch(function() { return {}; }),
  fetch('ukv_meta.json?_='   + Date.now()).then(function(r) { return r.json(); }).catch(function() { return null; })
]).then(function(res) {
  if (!res[1] || !res[1].runs || !res[1].runs.length) {
    document.getElementById('loadingState').style.display = 'none';
    document.getElementById('errorState').style.display   = '';
    return;
  }
  init(res[0], res[1]);
}).catch(function() {
  document.getElementById('loadingState').style.display = 'none';
  document.getElementById('errorState').style.display   = '';
});
