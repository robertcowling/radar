# UK Radar Visualisation — Frontend Recreation Guide

This document describes the complete frontend implementation of the UK radar/satellite animation app so it can be recreated in another project.

---

## Stack

| Concern | Choice |
|---------|--------|
| Map | Leaflet.js v1.9.4 (CDN) |
| Basemap | CARTO Voyager raster tiles |
| Fonts | Google Fonts — Roboto 300/400/500/700 |
| Satellite imagery | EUMETSAT EUMETView WMS (public, no auth) |
| Radar imagery | Pre-rendered PNGs served as static files |
| Manifest | `frames.json` — one JSON array, polled every 5 min |

---

## Data Format — `frames.json`

The frontend expects a JSON array where each element is one 15-minute frame:

```json
[
  {
    "time": "Wed 26 Mar 2025 14:00 UTC",
    "url": "static/radar/202503261400_radar_rainrate_composite_1km_UK.png",
    "sat_url_bw":  "static/sat/202503261400_radar_rainrate_composite_1km_UK_sat_bw.jpg",
    "sat_url_vis": "static/sat/202503261400_radar_rainrate_composite_1km_UK_sat_vis.png",
    "sat_url_ir":  "static/sat/202503261400_radar_rainrate_composite_1km_UK_sat_ir.jpg"
  },
  ...
]
```

- **`time`** — UTC string in exactly this format: `"Www DD Mmm YYYY HH:MM UTC"` (weekday optional — the parser handles 4- or 5-token forms).
- **`url`** — radar PNG (RGBA, transparent where no rain).
- **`sat_url_bw`** — GeoColour JPEG (day/night, always present for recent frames).
- **`sat_url_vis`** — HRFI VIS PNG (daytime only — omit or leave absent at night).
- **`sat_url_ir`** — HRFI IR JPEG (day/night, always present for recent frames).

Satellite fields are optional per-frame. If absent the UI simply doesn't update the satellite layer for that frame.

---

## Geographic Bounds

Two separate bounding boxes are used — radar is smaller (Met Office composite extent), satellite is 40% larger with the same centre (-4.0°, 55.25°):

```javascript
// Leaflet [SW, NE] = [[lat_min, lon_min], [lat_max, lon_max]]
var bounds    = [[49.0, -11.5], [61.5,  3.5]];  // radar
var satBounds = [[46.5, -14.5], [64.0,  6.5]];  // satellite
```

The satellite domain must match the WMS bbox used when downloading imagery — see `process_latest.py` constants `SAT_LAT_MIN/MAX`, `SAT_LON_MIN/MAX`.

---

## HTML Structure

```html
<!DOCTYPE html>
<html>
<head>
    <title>UK Radar on OpenStreetMap</title>
    <meta charset="utf-8" />
    <link rel="icon" type="image/png" href="favicon.png">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link href="https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
        integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
    <style>/* ... see CSS section ... */</style>
</head>
<body>
    <div id="map"></div>
    <!-- Info panel (top-right) -->
    <div class="info-panel surface" id="infoPanel">...</div>
    <!-- Player bar (bottom-centre) -->
    <div class="player-panel surface" id="controlsPanel">...</div>
    <script>/* ... see JavaScript section ... */</script>
</body>
</html>
```

### Info Panel HTML

```html
<div class="info-panel surface" id="infoPanel">
    <div class="collapsed-top-row">
        <div id="collapsedTime">Loading...</div>
        <button id="infoToggle" onclick="toggleInfo()" title="Toggle Info">
            <svg id="infoIcon" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="12" y1="16" x2="12" y2="12"></line>
                <line x1="12" y1="8" x2="12.01" y2="8"></line>
            </svg>
        </button>
    </div>

    <div id="infoContentTop" class="hide-collapsed">
        <h3 style="margin-top:0;">Rainfall Radar</h3>
        <div class="time-box">
            <p id="tzLabel" style="margin:0; font-size:11px; text-transform:uppercase; font-weight:700; color:#1a73e8; letter-spacing:0.5px;">Time (GMT)</p>
            <div id="timeDisplay" style="font-size:22px; font-weight:700; color:#202124; margin-top:4px;">Loading...</div>
        </div>
    </div>

    <div class="cbar-container">
        <p style="font-weight:500; margin-bottom:10px; font-size:12px; color:#3c4043;">Rainfall Rate (mm/hr)</p>
        <div class="cbar" id="radarCbar"></div>
        <div class="cbar-labels" id="radarLabels"></div>
    </div>

    <div id="infoContentBottom" class="hide-collapsed">
        <div class="opacity-control" style="margin-top:20px;">
            <label class="custom-checkbox">
                <input type="checkbox" id="boundaryToggle" checked onchange="toggleBoundaries(this.checked)">
                UK regions
            </label>
        </div>

        <div class="opacity-control" style="margin-top:8px;">
            <label class="custom-checkbox">
                <input type="checkbox" id="radarToggle" checked onchange="toggleRadar(this.checked)">
                Radar
            </label>
        </div>

        <p style="margin:14px 0 6px; font-size:11px; font-weight:700; color:#3c4043; text-transform:uppercase; letter-spacing:0.5px;">Satellite</p>
        <div style="margin-bottom:24px;">
            <label class="custom-checkbox" style="margin-bottom:4px;">
                <input type="radio" name="satLayer" value="none" checked onchange="onSatLayerChange('none')">
                None
            </label>
            <label class="custom-checkbox" style="margin-bottom:4px;">
                <input type="radio" name="satLayer" value="geo" onchange="onSatLayerChange('geo')">
                GeoColour (day/night)
            </label>
            <label class="custom-checkbox" style="margin-bottom:4px;">
                <input type="radio" name="satLayer" value="vis" onchange="onSatLayerChange('vis')">
                Black &amp; White (day)
            </label>
            <label class="custom-checkbox" style="margin-bottom:0;">
                <input type="radio" name="satLayer" value="ir" onchange="onSatLayerChange('ir')">
                Infra Red (day/night)
            </label>
        </div>

        <p style="font-size:11px; margin-bottom:4px; line-height:1.5;">
            <strong>Radar:</strong> <a href="https://www.metoffice.gov.uk/services/data/share-your-data" target="_blank" style="color:#1a73e8;">Met Office</a> ODIM H5 composite (1km) — Open Government Licence v3.0<br>
            <strong>Satellite:</strong> &copy; <a href="https://www.eumetsat.int" target="_blank" style="color:#1a73e8;">EUMETSAT</a> MTG FCI<br>
            &nbsp;&nbsp;<a href="https://view.eumetsat.int/productviewer/productDetails/mtg_fd:rgb_geocolour" target="_blank" style="color:#1a73e8;">GeoColour RGB</a> &middot;
            <a href="https://view.eumetsat.int/productviewer/productDetails/mtg_fd:vis06_hrfi" target="_blank" style="color:#1a73e8;">HRFI VIS0.6</a> &middot;
            <a href="https://view.eumetsat.int/productviewer/productDetails/mtg_fd:ir105_hrfi" target="_blank" style="color:#1a73e8;">HRFI IR10.5</a>
        </p>
    </div>
</div>
```

### Player Panel HTML

```html
<div class="player-panel surface" id="controlsPanel">
    <div class="control-group play-group">
        <span class="control-label" style="opacity:0; pointer-events:none;">Play</span>
        <button class="btn" id="playBtn" onclick="togglePlay()">
            <svg id="playIcon" width="16" height="16" viewBox="0 0 24 24" fill="currentColor" stroke="none">
                <polygon points="5 3 19 12 5 21 5 3"></polygon>
            </svg>
            <span id="playText">Play</span>
        </button>
    </div>

    <div class="timeline-container">
        <div class="timeline-track">
            <div id="loadedProgress" class="loaded-progress"></div>
        </div>
        <div class="timeline-ticks" id="timelineTicks"></div>
        <input type="range" id="timeSlider" min="0" oninput="onSliderChange(this.value)">
    </div>

    <div class="control-group">
        <span class="control-label">Show</span>
        <select id="durationSelect" onchange="updateDuration(this.value)">
            <option value="3" selected>Last 3 Hours</option>
            <option value="6">Last 6 Hours</option>
            <option value="12">Last 12 Hours</option>
            <option value="24">Last 24 Hours</option>
        </select>
    </div>

    <div class="control-group">
        <span class="control-label">Speed</span>
        <select id="speedSelect" onchange="updateSpeed(this.value)">
            <option value="360">Slow</option>
            <option value="135" selected>Normal</option>
            <option value="70">Fast</option>
            <option value="35">Turbo</option>
        </select>
    </div>
</div>
```

---

## CSS

Full stylesheet — paste inside `<style>` in `<head>`:

```css
body {
    padding: 0;
    margin: 0;
    font-family: 'Roboto', sans-serif;
    background: #f8f9fa;
    color: #3c4043;
}

#map {
    width: 100vw;
    height: 100vh;
}

/* Material-style card surface */
.surface {
    background: white;
    border-radius: 12px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.12), 0 2px 6px rgba(0,0,0,0.08);
    transition: box-shadow 0.3s ease;
}
.surface:hover {
    box-shadow: 0 12px 32px rgba(0,0,0,0.16), 0 4px 10px rgba(0,0,0,0.1);
}

/* Info panel — expanded */
.info-panel {
    position: absolute;
    top: 20px;
    right: 20px;
    padding: 24px;
    z-index: 1000;
    width: 270px;
    transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
}

/* Info panel — collapsed (pill) */
.info-panel.collapsed {
    width: 220px;
    height: auto;
    padding: 8px 14px 10px 14px;
    border-radius: 20px;
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: 4px;
    overflow: visible;
}
.info-panel.collapsed .collapsed-top-row {
    display: flex;
    flex-direction: row;
    align-items: center;
    width: 100%;
    justify-content: space-between;
    gap: 4px;
}

/* Collapsed mini-time display */
#collapsedTime {
    display: none;
    font-size: 13px;
    font-weight: 500;
    color: #1a73e8;
    white-space: nowrap;
}
.info-panel.collapsed #collapsedTime { display: block; }

/* Colour bar adjustments in collapsed mode */
.info-panel.collapsed .cbar-container {
    margin: 0;
    width: 100%;
    display: flex;
    flex-direction: column;
    justify-content: center;
    box-sizing: border-box;
}
.info-panel.collapsed .cbar-container p { display: none; }
.info-panel.collapsed .cbar-container .cbar-labels {
    position: relative;
    height: 11px;
    font-size: 8.5px;
    margin-top: 3px;
    line-height: 1;
    width: 100%;
}
.info-panel.collapsed .cbar {
    height: 6px;
    margin: 0;
    border-radius: 3px;
}

/* Toggle button */
#infoToggle {
    background: transparent;
    border: none;
    cursor: pointer;
    padding: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #1a73e8;
    margin-bottom: 0;
    transition: transform 0.2s;
}
#infoToggle:hover { transform: scale(1.1); }
.info-panel #infoToggle { position: absolute; top: 16px; right: 16px; }
.info-panel.collapsed #infoToggle { position: static; }
.info-panel h3 { margin-right: 30px; margin-top: 0; }

/* Hide elements in collapsed mode */
.info-panel.collapsed .hide-collapsed { display: none !important; }

/* Player panel */
.player-panel {
    position: absolute;
    bottom: 30px;
    left: 50%;
    transform: translateX(-50%);
    padding: 12px 20px;
    z-index: 1000;
    width: 95%;
    max-width: 950px;
    display: flex;
    align-items: center;
    gap: 16px;
}

/* Play/Pause button */
.btn {
    background: #1a73e8;
    color: white;
    border: none;
    padding: 10px 24px;
    border-radius: 24px;
    cursor: pointer;
    font-weight: 500;
    font-family: 'Roboto', sans-serif;
    font-size: 14px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.2);
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    min-width: 100px;
    max-width: 140px;
    z-index: 4;
}
.btn:hover { background: #1557b0; box-shadow: 0 4px 8px rgba(0,0,0,0.25); transform: translateY(-1px); }
.btn:active { box-shadow: 0 1px 2px rgba(0,0,0,0.2); transform: translateY(0); }

/* Dropdowns */
select {
    appearance: none;
    background: #f1f3f4;
    border: none;
    padding: 10px 36px 10px 20px;
    border-radius: 24px;
    font-family: 'Roboto', sans-serif;
    font-size: 14px;
    color: #202124;
    font-weight: 500;
    cursor: pointer;
    background-image: url("data:image/svg+xml,%3Csvg width='10' height='6' viewBox='0 0 10 6' fill='none' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M1 1L5 5L9 1' stroke='%235F6368' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 16px center;
    outline: none;
    transition: background 0.2s;
}
select:hover { background-color: #e8eaed; }

/* Timeline */
.timeline-container {
    position: relative;
    flex-grow: 1;
    height: 48px;
    display: flex;
    align-items: center;
}
.timeline-track {
    position: absolute;
    top: 50%;
    left: 9px;
    right: 9px;
    height: 6px;
    margin-top: -3px;
    background: #e8eaed;
    border-radius: 3px;
    z-index: 1;
    overflow: hidden;
    pointer-events: none;
}
.loaded-progress {
    height: 100%;
    width: 0%;
    background: #bdc1c6;
    transition: width 0.3s ease;
}
.timeline-ticks {
    position: absolute;
    top: 50%;
    left: 8px;
    right: 8px;
    height: 20px;
    transform: translateY(-50%);
    pointer-events: none;
    z-index: 2;
}
.tick {
    position: absolute;
    width: 2px;
    height: 6px;
    background: #dadce0;
    top: 7px;
    transform: translateX(-50%);
    border-radius: 1px;
}
.tick.hour { height: 10px; top: 5px; background: #bdc1c6; }
.tick-label {
    position: absolute;
    top: 24px;
    font-size: 11px;
    font-weight: 500;
    color: #80868b;
    transform: translateX(-50%);
    white-space: nowrap;
}
.tick.today-mark {
    width: auto;
    background: transparent;
    color: #1a73e8;
    top: -22px;
    font-size: 12px;
    display: flex;
    align-items: center;
    flex-direction: column;
    gap: 2px;
}

/* Custom range slider */
input[type=range] {
    -webkit-appearance: none;
    appearance: none;
    width: 100%;
    background: transparent;
    z-index: 3;
    margin: 0;
    position: relative;
}
input[type=range]:focus { outline: none; }
input[type=range]::-webkit-slider-runnable-track {
    width: 100%;
    height: 6px;
    cursor: pointer;
    background: transparent;
    border-radius: 3px;
}
input[type=range]::-webkit-slider-thumb {
    height: 18px;
    width: 18px;
    border-radius: 50%;
    background: #1a73e8;
    cursor: pointer;
    -webkit-appearance: none;
    margin-top: -6px;
    box-shadow: 0 2px 4px rgba(0,0,0,0.3);
    transition: transform 0.1s, box-shadow 0.1s;
    border: 2px solid white;
}
input[type=range]::-webkit-slider-thumb:hover {
    transform: scale(1.15);
    box-shadow: 0 3px 6px rgba(0,0,0,0.4);
}

/* Typography */
h3 { margin: 0 0 16px 0; font-size: 18px; color: #202124; font-weight: 500; letter-spacing: 0.2px; }
p  { margin: 0 0 6px 0; font-size: 13px; color: #5f6368; }

/* Time display box */
.time-box {
    background: #f8f9fa;
    border-left: 4px solid #1a73e8;
    padding: 12px 16px;
    margin: 20px 0;
    border-radius: 8px;
    text-align: left;
}

/* Colour bar */
.cbar-container { margin-top: 20px; }
.cbar { display: flex; height: 10px; width: 100%; border-radius: 5px; overflow: hidden; margin-bottom: 8px; }
.cbar-block { flex-grow: 1; }
.cbar-labels { position: relative; height: 12px; width: 100%; font-size: 10px; color: #5f6368; font-weight: 500; margin-top: 2px; }
.cbar-label { position: absolute; transform: translateX(-50%); white-space: nowrap; }

/* Controls */
.opacity-control { margin-top: 20px; }
.opacity-control label { font-size: 13px; font-weight: 500; color: #3c4043; display: block; margin-bottom: 8px; }
.control-group { display: flex; flex-direction: column; align-items: flex-start; gap: 3px; }
.control-group.play-group { align-items: center; }
.control-label { font-size: 10px; font-weight: 600; color: #80868b; letter-spacing: 0.5px; padding-left: 8px; }

/* Custom checkbox / radio */
.custom-checkbox { display: flex; align-items: center; cursor: pointer; font-size: 13px; font-weight: 500; color: #3c4043; }
.custom-checkbox input { cursor: pointer; width: 16px; height: 16px; margin-right: 10px; accent-color: #1a73e8; }

/* Prevent upscaling blur on radar tiles */
.sharp-radar {
    image-rendering: -webkit-optimize-contrast;
    image-rendering: crisp-edges;
    image-rendering: pixelated;
}

/* Mobile */
@media (max-width: 768px) {
    .player-panel {
        width: calc(100% - 20px);
        padding: 10px 12px;
        gap: 8px;
        bottom: 8px;
        box-sizing: border-box;
        flex-wrap: wrap;
    }
    .info-panel { top: 10px; right: 10px; left: auto; width: auto; padding: 20px; }
    .info-panel.collapsed { width: 200px; height: auto; border-radius: 18px; padding: 8px 12px 10px 12px; top: 15px; right: 15px; }
    .info-panel:not(.collapsed) {
        width: calc(100% - 30px);
        left: 15px; right: 15px; top: 15px;
        box-sizing: border-box;
        max-height: calc(100vh - 120px);
        overflow-y: auto;
        -webkit-overflow-scrolling: touch;
    }
    .timeline-container { width: 100%; order: 4; margin-top: 5px; }
    .control-group { flex: 1; }
    .control-group.play-group { flex: 0 1 auto; min-width: 80px; }
    #durationSelect, #speedSelect { width: 100%; font-size: 13px; padding: 8px 12px; box-sizing: border-box; }
    #playBtn { width: 100%; padding: 8px 16px; min-width: 0; max-width: none; }
}
```

---

## JavaScript

### 1. State variables & Map init

```javascript
var allFrames = [];          // full manifest from frames.json
var frames = [];             // current slice (filtered by duration)
var currentFrameIdx = 0;
var playing = false;
var playInterval;
var playSpeed = 135;         // ms per frame (normal speed)

var map = L.map('map', { zoomControl: false }).setView([54.5, -3.5], 6);
L.control.zoom({ position: 'topleft' }).addTo(map);

L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', {
    maxZoom: 20,
    attribution: '&copy; OpenStreetMap contributors &copy; CARTO'
}).addTo(map);
```

### 2. Overlays

```javascript
var bounds    = [[49.0, -11.5], [61.5,  3.5]];  // radar
var satBounds = [[46.5, -14.5], [64.0,  6.5]];  // satellite

var radarOverlay = L.imageOverlay('', bounds, {
    opacity: 0.7,
    interactive: false,
    className: 'sharp-radar'
}).addTo(map);

// UK region boundaries from local GeoJSON
var regionLayer;
fetch('uk_regions.geojson')
    .then(res => res.json())
    .then(data => {
        regionLayer = L.geoJSON(data, {
            style: { color: '#5f6368', weight: 1, fillOpacity: 0 },
            interactive: false
        }).addTo(map);
    })
    .catch(err => console.log("Could not load regions:", err));

function toggleBoundaries(show) {
    if (!regionLayer) return;
    if (show) regionLayer.addTo(map);
    else map.removeLayer(regionLayer);
}

function toggleRadar(show) {
    if (show) {
        radarOverlay.addTo(map);
        radarOverlay.bringToFront();
        if (regionLayer) regionLayer.bringToFront();
    } else {
        map.removeLayer(radarOverlay);
    }
}
```

### 3. Satellite layer system

Satellite layers are lazily created — an overlay is only instantiated the first time a given radio option is selected.

```javascript
var satOverlays = {};
// Maps radio value → frames.json key
var satUrlKeys = { geo: 'sat_url_bw', vis: 'sat_url_vis', ir: 'sat_url_ir' };

function onSatLayerChange(val) {
    Object.keys(satOverlays).forEach(function(k) {
        if (map.hasLayer(satOverlays[k])) map.removeLayer(satOverlays[k]);
    });
    if (val === 'none') return;
    if (!satOverlays[val]) {
        satOverlays[val] = L.imageOverlay('', satBounds, { opacity: 1.0, interactive: false });
    }
    satOverlays[val].addTo(map);
    satOverlays[val].bringToBack();           // sat behind radar
    if (map.hasLayer(radarOverlay)) radarOverlay.bringToFront();
    if (regionLayer) regionLayer.bringToFront();
    if (frames.length > 0) setFrame(currentFrameIdx);
    preloadVisibleFrames();
}
```

### 4. UK time display (BST/GMT, zero cost)

The browser's `Intl.DateTimeFormat` with `timeZone: 'Europe/London'` handles the BST↔GMT switch automatically. No manual DST logic needed.

```javascript
const _MONTHS = {Jan:0,Feb:1,Mar:2,Apr:3,May:4,Jun:5,Jul:6,Aug:7,Sep:8,Oct:9,Nov:10,Dec:11};

function parseUTCStr(s) {
    // Handles "Wed 26 Mar 2025 14:00 UTC" (5 tokens) or "26 Mar 2025 14:00 UTC" (4 tokens)
    const p = s.replace(' UTC','').split(' ');
    const hasDay = p.length === 5;
    const [day, mon, yr, hm] = hasDay ? p.slice(1) : p;
    const [hr, mn] = hm.split(':');
    return new Date(Date.UTC(+yr, _MONTHS[mon], +day, +hr, +mn));
}

const _ukFmt = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Europe/London',
    weekday: 'short', day: '2-digit', month: 'short', year: 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: false
});
const _ukTzFmt = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Europe/London', timeZoneName: 'short'
});

function ukTzAbbr(date) {
    return _ukTzFmt.formatToParts(date).find(p => p.type === 'timeZoneName').value; // "GMT" or "BST"
}
function formatUK(date) {
    const parts = _ukFmt.formatToParts(date);
    const get = t => parts.find(p => p.type === t).value;
    return `${get('weekday')} ${get('day')} ${get('month')} ${get('year')} ${get('hour')}:${get('minute')}`;
}
```

### 5. Frame rendering

```javascript
var slider = document.getElementById('timeSlider');

function setFrame(idx) {
    currentFrameIdx = idx;
    var frameData = frames[idx];
    radarOverlay.setUrl(frameData.url);
    updateFrameUI(idx);

    var satVal = document.querySelector('input[name="satLayer"]:checked').value;
    var satKey = satUrlKeys[satVal];
    if (satKey && satOverlays[satVal] && map.hasLayer(satOverlays[satVal])) {
        if (frameData[satKey]) satOverlays[satVal].setUrl(frameData[satKey]);
        // If no sat data for this frame (e.g. VIS at night), overlay keeps last URL
    }
}

function updateFrameUI(idx) {
    const ukDate = parseUTCStr(frames[idx].time);
    const tz = ukTzAbbr(ukDate);
    const formatted = formatUK(ukDate);                     // "Wed 26 Mar 2025 14:00"
    document.getElementById('timeDisplay').innerText = formatted;
    document.getElementById('tzLabel').innerText = `Time (${tz})`;

    // Collapsed pill: "Wed 26 Mar 14:00"
    const parts = formatted.split(' ');
    document.getElementById('collapsedTime').innerText =
        `${parts[0]} ${parts[1]} ${parts[2]} ${parts[4]}`;

    slider.value = idx;
}
```

### 6. Manifest loading & polling

```javascript
function fetchFrames(isInitial) {
    fetch('frames.json?_cb=' + Date.now())          // cache-bust every call
        .then(r => r.json())
        .then(data => {
            if (!data || data.length === 0) {
                if (isInitial) document.getElementById('progressText').innerText = "No data available.";
                return;
            }

            const wasAtLatest = !isInitial && (currentFrameIdx === frames.length - 1);
            const prevLatestTime = allFrames.length ? allFrames[allFrames.length - 1].time : null;
            allFrames = data;

            if (isInitial) {
                if (window.innerWidth <= 768) {
                    document.getElementById('infoPanel').classList.add('collapsed');
                }
                updateDuration(document.getElementById('durationSelect').value);
            } else {
                const newLatestTime = allFrames[allFrames.length - 1].time;
                if (newLatestTime !== prevLatestTime) {
                    updateDuration(document.getElementById('durationSelect').value);
                    if (wasAtLatest || currentFrameIdx >= frames.length - 1) {
                        setFrame(frames.length - 1);
                    }
                }
            }
        })
        .catch(err => console.error("Manifest load error:", err));
}

fetchFrames(true);
setInterval(function() { fetchFrames(false); }, 5 * 60 * 1000);  // poll every 5 min
```

### 7. Duration filter

```javascript
function updateDuration(hours) {
    var numFrames = parseInt(hours) * 4;     // 15-min frames → 4 per hour
    if (numFrames > allFrames.length) numFrames = allFrames.length;
    frames = allFrames.slice(-numFrames);    // most-recent N frames
    slider.max = frames.length - 1;
    updateTimelineTicks();
    preloadVisibleFrames();
    setFrame(frames.length - 1);            // jump to newest
}

function onSliderChange(val) {
    if (playing) togglePlay();
    setFrame(parseInt(val));
}
```

### 8. Preloader with progress bar

Preloads all radar + satellite images for the current duration window. Starts playback automatically when all images are cached.

```javascript
var _preloadGeneration = 0;

function preloadVisibleFrames() {
    _preloadGeneration++;
    var gen = _preloadGeneration;
    var pb = document.getElementById('loadedProgress');
    if (pb) { pb.style.opacity = '1'; pb.style.width = '0%'; pb.style.transition = 'width 0.2s ease, opacity 0.5s'; }
    if (frames.length === 0) return;

    var urlsToLoad = [];
    var satVal = document.querySelector('input[name="satLayer"]:checked').value;
    var satKey = satUrlKeys[satVal];

    frames.forEach(function(f) {
        urlsToLoad.push(f.url);
        if (satKey && f[satKey]) urlsToLoad.push(f[satKey]);
    });

    var total = urlsToLoad.length;
    if (total === 0) return;
    var loadedCount = 0;

    urlsToLoad.forEach(function(url) {
        var img = new Image();
        var done = function() {
            if (gen !== _preloadGeneration) return;     // stale; newer preload started
            loadedCount++;
            if (pb) pb.style.width = ((loadedCount / total) * 100) + '%';
            if (loadedCount === total) {
                if (!playing) togglePlay();
                setTimeout(function() { if (pb && gen === _preloadGeneration) pb.style.opacity = '0'; }, 2000);
            }
        };
        img.onload = done;
        img.onerror = done;
        img.src = url;
    });
}
```

### 9. Player controls

```javascript
function playStep() {
    if (!playing) return;
    var nextIdx = currentFrameIdx + 1;
    var delay = playSpeed;
    if (nextIdx >= frames.length) {
        nextIdx = 0;                            // loop
    } else if (nextIdx === frames.length - 1) {
        delay = 1000;                           // 1 s dwell on newest frame
    }
    setFrame(nextIdx);
    playInterval = setTimeout(playStep, delay);
}

function togglePlay() {
    var icon = document.getElementById('playIcon');
    var text = document.getElementById('playText');

    if (playing) {
        clearTimeout(playInterval);
        icon.innerHTML = '<polygon points="5 3 19 12 5 21 5 3"></polygon>';
        text.innerText = "Play";
        playing = false;
    } else {
        if (currentFrameIdx >= frames.length - 1) setFrame(0);
        icon.innerHTML = '<rect x="6" y="4" width="4" height="16"></rect><rect x="14" y="4" width="4" height="16"></rect>';
        text.innerText = "Pause";
        playing = true;
        var initialDelay = (currentFrameIdx === frames.length - 1) ? 1000 : playSpeed;
        playInterval = setTimeout(playStep, initialDelay);
    }
}

function updateSpeed(newSpeed) {
    playSpeed = parseInt(newSpeed);
    if (playing) {
        clearTimeout(playInterval);
        var delay = (currentFrameIdx === frames.length - 1) ? 1000 : playSpeed;
        playInterval = setTimeout(playStep, delay);
    }
}
```

### 10. Info panel collapse

```javascript
function toggleInfo() {
    const panel = document.getElementById('infoPanel');
    const icon  = document.getElementById('infoIcon');
    panel.classList.toggle('collapsed');

    if (panel.classList.contains('collapsed')) {
        // Info circle icon
        icon.innerHTML = '<circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line>';
    } else {
        // X / close icon
        icon.innerHTML = '<line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line>';
    }
}
```

### 11. Timeline ticks

Draws day markers, midnight (red), noon (yellow), and hourly ticks with adaptive label frequency.

```javascript
function updateTimelineTicks() {
    const ticksContainer = document.getElementById('timelineTicks');
    ticksContainer.innerHTML = '';
    const max = frames.length - 1;
    if (max <= 0) return;

    let lastDay = null, lastDayPercent = -100;
    const labelInterval = frames.length > 50 ? 3 : (frames.length > 20 ? 2 : 1);

    frames.forEach((frame, idx) => {
        if (!frame.time || frame.time === "Unknown Date") return;

        const ukDate  = parseUTCStr(frame.time);
        const ukParts = _ukFmt.formatToParts(ukDate);
        const get = t => ukParts.find(p => p.type === t).value;
        const dateStr = `${get('weekday')} ${get('day')} ${get('month')}`;
        const timeStr = `${get('hour')}:${get('minute')}`;
        const hourNum = parseInt(get('hour'));
        const percent = (idx / max) * 100;

        // Day boundary label
        if (dateStr !== lastDay) {
            if (percent - lastDayPercent > 12) {
                const todayMark = document.createElement('div');
                todayMark.className = 'tick today-mark';
                todayMark.style.left = `${Math.max(percent, 4)}%`;
                const svgCheck = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#1a73e8" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>`;
                todayMark.innerHTML = `${svgCheck} <span style="font-weight:700;">${dateStr}</span>`;
                ticksContainer.appendChild(todayMark);
                lastDayPercent = percent;
            }
            lastDay = dateStr;
        }

        if (timeStr === '00:00') {
            // Midnight — tall red tick, no label
            const tick = document.createElement('div');
            Object.assign(tick.style, { left:`${percent}%`, height:'14px', top:'3px', background:'#ea4335', width:'2px', zIndex:'3' });
            tick.className = 'tick';
            ticksContainer.appendChild(tick);
        } else if (timeStr === '12:00') {
            // Noon — yellow tick + label
            const tick = document.createElement('div');
            Object.assign(tick.style, { left:`${percent}%`, height:'12px', top:'4px', background:'#fbbc04', width:'2px', zIndex:'2' });
            tick.className = 'tick';
            ticksContainer.appendChild(tick);
            const label = document.createElement('div');
            label.className = 'tick-label';
            label.style.left = `${percent}%`;
            label.innerText = '12:00';
            ticksContainer.appendChild(label);
        } else if (timeStr.endsWith(':00')) {
            // Other hours — standard tick, conditional label
            const tick = document.createElement('div');
            tick.className = 'tick hour';
            tick.style.left = `${percent}%`;
            ticksContainer.appendChild(tick);
            if (hourNum % labelInterval === 0 && dateStr === lastDay && percent > 2 && percent < 98) {
                const label = document.createElement('div');
                label.className = 'tick-label';
                label.style.left = `${percent}%`;
                label.innerText = timeStr;
                ticksContainer.appendChild(label);
            }
        } else if (timeStr.endsWith(':30') && frames.length <= 48) {
            // Half-hour minor ticks only for short windows
            const tick = document.createElement('div');
            tick.className = 'tick';
            tick.style.left = `${percent}%`;
            ticksContainer.appendChild(tick);
        }
    });
}
```

### 12. Colour bar

```javascript
var radar_colors = [
    '#e1f5fe', '#81d4fa', '#03a9f4', '#01579b', '#2e7d32',
    '#fbc02d', '#ef6c00', '#c62828', '#6a1b9a'
];
var cbarHtml = '';
for (var i = 0; i < radar_colors.length; i++) {
    cbarHtml += '<div class="cbar-block" style="background-color:' + radar_colors[i] + ';"></div>';
}
document.getElementById('radarCbar').innerHTML = cbarHtml;

var radar_labels = [0.1, 0.5, 1, 2, 4, 8, 16, 32, 64];
document.getElementById('radarLabels').innerHTML =
    radar_labels.map((l, i) => `<span class="cbar-label" style="left:${i * 11.11}%">${l}</span>`).join('');
```

These colours match the thresholds in `process_latest.py` (`bounds` array: 0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64 mm/hr).

---

## Satellite layers — EUMETSAT WMS

All three satellite layers come from the same public endpoint (no authentication required):

```
https://view.eumetsat.int/geoserver/ows
```

| UI label | Radio value | `frames.json` key | WMS layer | Format | Style | Notes |
|----------|-------------|-------------------|-----------|--------|-------|-------|
| GeoColour (day/night) | `geo` | `sat_url_bw` | `mtg_fd:rgb_geocolour` | `image/jpeg` | *(none)* | Cosine-weighted day↔night blend; always available |
| Black & White (day) | `vis` | `sat_url_vis` | `mtg_fd:vis06_hrfi` | `image/png` | *(none)* | 0.5 km visible; absent at night |
| Infra Red (day/night) | `ir` | `sat_url_ir` | `mtg_fd:ir105_hrfi` | `image/jpeg` | `mtg_fd_ir105_hrfi_style_01` | 1 km thermal IR; always available |

WMS request parameters:
- `service=WMS&request=GetMap&version=1.3.0`
- `crs=EPSG:3857` — **must** be Web Mercator, not 4326, to align with Leaflet tiles
- `bbox={m_xmin},{m_ymin},{m_xmax},{m_ymax}` — Mercator metres, derived from the satellite lat/lon domain
- `width=1200&height=1000` — pixel dimensions (match aspect ratio of `SAT_LAT/LON` bounds)
- PNG is used for the VIS layer to avoid JPEG artefacts on high-contrast greyscale imagery

---

## Static Assets

| File | Purpose |
|------|---------|
| `uk_regions.geojson` | UK region boundary polygons (Leaflet GeoJSON layer) |
| `favicon.png` | Browser tab icon |
| `static/radar/*.png` | Pre-rendered radar frames |
| `static/sat/*.jpg` / `*.png` | Downloaded satellite frames |
| `frames.json` | Manifest array — regenerated by `process_latest.py` |
