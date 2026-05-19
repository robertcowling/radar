#!/usr/bin/env python3
import sys, re
sys.stdout.reconfigure(encoding='utf-8')

path = "rain/index.html"
with open(path, 'r', encoding='utf-8') as f:
    c = f.read()

original_len = len(c)
fixes = 0

def apply(label, old, new, text):
    global fixes
    n = text.count(old)
    if n == 0:
        print(f"  MISS: {label}")
        frag = old.strip()[:60]
        i = text.find(frag)
        if i >= 0:
            print(f"    fragment at {i}: {repr(text[i:i+120])}")
    else:
        print(f"  OK ({n}): {label}")
        fixes += n
    return text.replace(old, new)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION A — Slider CSS overhaul
# ═══════════════════════════════════════════════════════════════════════════════

# A1: Container 76→84px
c = apply("container 84px",
    "            height: 76px;",
    "            height: 84px;",
    c)

# A2: Track top 54%→60%
c = apply("track top 60%",
    "        .timeline-track {\n"
    "            position: absolute;\n"
    "            top: 54%;\n"
    "            left: 9px; right: 9px;",
    "        .timeline-track {\n"
    "            position: absolute;\n"
    "            top: 60%;\n"
    "            left: 9px; right: 9px;",
    c)

# A3: Ticks top 54%→60%
c = apply("ticks top 60%",
    "        .timeline-ticks {\n"
    "            position: absolute;\n"
    "            top: 54%;\n"
    "            left: 8px; right: 8px;",
    "        .timeline-ticks {\n"
    "            position: absolute;\n"
    "            top: 60%;\n"
    "            left: 8px; right: 8px;",
    c)

# A4: Range-input top 54%→60%, z-index 3→5
c = apply("range-input top 60% z5",
    "        .range-input {\n"
    "            position: absolute; width: 100%;\n"
    "            top: 54%; transform: translateY(-50%);\n"
    "            z-index: 3;\n"
    "        }",
    "        .range-input {\n"
    "            position: absolute; width: 100%;\n"
    "            top: 60%; transform: translateY(-50%);\n"
    "            z-index: 5;\n"
    "        }",
    c)

# A5: Today-mark top -30→-40px, font 12→11px
c = apply("today-mark top -40px",
    "            top: -30px;\n"
    "            font-size: 12px;",
    "            top: -40px;\n"
    "            font-size: 11px;",
    c)

# A6: Grab — from bottom positioning to top/centered-on-track, height 22→18, add z-index:4
c = apply("grab top-centered z4",
    "        .range-grab {\n"
    "            position: absolute;\n"
    "            bottom: calc(100% + 4px);\n"
    "            left: 50%; margin-left: -14px;\n"
    "            width: 28px; height: 22px;",
    "        .range-grab {\n"
    "            position: absolute;\n"
    "            top: 50%; margin-top: -9px;\n"
    "            left: 50%; margin-left: -14px;\n"
    "            width: 28px; height: 18px;\n"
    "            z-index: 4;",
    c)

# A7: Grab-line height 10→8px
c = apply("grab-line 8px",
    ".grab-line { width: 2px; height: 10px; background: white; border-radius: 1px; }",
    ".grab-line { width: 2px; height: 7px; background: white; border-radius: 1px; }",
    c)

# A8: Start thumb — taller, shifted upward (thumb extends above grab)
c = apply("start thumb tall",
    "#sliderStart::-webkit-slider-thumb {\n"
    "            background: linear-gradient(90deg, transparent 5px, #5f6368 5px, #5f6368 9px, transparent 9px);\n"
    "            height: 22px; width: 14px; margin-top: -8px;\n"
    "        }",
    "#sliderStart::-webkit-slider-thumb {\n"
    "            background: linear-gradient(90deg, transparent 5px, #5f6368 5px, #5f6368 9px, transparent 9px);\n"
    "            height: 36px; width: 14px; margin-top: -28px;\n"
    "        }",
    c)

# A9: End thumb — taller, shifted upward
c = apply("end thumb tall",
    "#sliderEnd::-webkit-slider-thumb {\n"
    "            background: linear-gradient(90deg, transparent 4px, #1a73e8 4px, #1a73e8 10px, transparent 10px);\n"
    "            height: 30px; width: 14px; margin-top: -12px;\n"
    "        }",
    "#sliderEnd::-webkit-slider-thumb {\n"
    "            background: linear-gradient(90deg, transparent 4px, #1a73e8 4px, #1a73e8 10px, transparent 10px);\n"
    "            height: 36px; width: 14px; margin-top: -28px;\n"
    "        }",
    c)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION B — Hyetograph SVG fixes
# ═══════════════════════════════════════════════════════════════════════════════

# B1: Increase padB from 14→22 for two-line x-axis labels
c = apply("hyeto padB 22",
    "            var padL = 22, padB = 14, padR = 2, padT = 4;",
    "            var padL = 22, padB = 22, padR = 2, padT = 4;",
    c)

# B2: X-axis: 12hr steps showing HH:00 always, plus DD Mon on midnight ticks
old_xaxis = (
    "            // X-axis labels every 12 hr\n"
    "            for (var i = 0; i <= max; i += 48) {\n"
    "                var lx = (padL + (i / max) * cW).toFixed(1);\n"
    "                var dt = new Date(slotToMs(i));\n"
    "                svg += '<line x1=\"' + lx + '\" y1=\"' + baseY + '\" x2=\"' + lx + '\" y2=\"' + (baseY + 3) +\n"
    "                       '\" stroke=\"#bdc1c6\" stroke-width=\"1\"/>';\n"
    "                var lbl = (dt.getHours() === 0 && dt.getMinutes() === 0)\n"
    "                    ? DAYS[dt.getDay()].substr(0,3)\n"
    "                    : String(dt.getHours()).padStart(2,'0') + ':00';\n"
    "                svg += '<text x=\"' + lx + '\" y=\"' + (baseY + 11) + '\" fill=\"#80868b\" font-size=\"8\" ' +\n"
    "                       'font-family=\"sans-serif\" text-anchor=\"middle\">' + lbl + '</text>';\n"
    "            }"
)
new_xaxis = (
    "            // X-axis labels every 12 hr (HH:00 always; date on midnight ticks)\n"
    "            for (var i = 0; i <= max; i += 48) {\n"
    "                var lx = (padL + (i / max) * cW).toFixed(1);\n"
    "                var dt = new Date(slotToMs(i));\n"
    "                svg += '<line x1=\"' + lx + '\" y1=\"' + baseY + '\" x2=\"' + lx + '\" y2=\"' + (baseY + 3) +\n"
    "                       '\" stroke=\"#bdc1c6\" stroke-width=\"1\"/>';\n"
    "                var tLbl = String(dt.getHours()).padStart(2,'0') + ':00';\n"
    "                svg += '<text x=\"' + lx + '\" y=\"' + (baseY + 10) + '\" fill=\"#80868b\" font-size=\"8\" ' +\n"
    "                       'font-family=\"sans-serif\" text-anchor=\"middle\">' + tLbl + '</text>';\n"
    "                if (dt.getHours() === 0) {\n"
    "                    var dLbl = String(dt.getDate()).padStart(2,'0') + ' ' + MONTHS[dt.getMonth()];\n"
    "                    svg += '<text x=\"' + lx + '\" y=\"' + (baseY + 19) + '\" fill=\"#5f6368\" font-size=\"7.5\" ' +\n"
    "                           'font-family=\"sans-serif\" text-anchor=\"middle\">' + dLbl + '</text>';\n"
    "                }\n"
    "            }"
)
c = apply("hyeto x-axis 12hr+date", old_xaxis, new_xaxis, c)

# B3: Stagger from/to labels vertically when they're close
old_labels = (
    "            svg += '<text x=\"' + fromTx.toFixed(1) + '\" y=\"' + (padT + 9) + '\" fill=\"#5f6368\" ' +\n"
    "                   'font-size=\"7.5\" font-family=\"sans-serif\">' + fromLbl + '</text>';\n"
    "            svg += '<text x=\"' + toTx.toFixed(1) + '\" y=\"' + (padT + 9) + '\" fill=\"#1a73e8\" ' +\n"
    "                   'font-size=\"7.5\" font-family=\"sans-serif\" text-anchor=\"end\">' + toLbl + '</text>';"
)
new_labels = (
    "            var labY1 = padT + 8;\n"
    "            var labY2 = (Math.abs(ex - sx) < 40) ? padT + 17 : padT + 8;\n"
    "            svg += '<text x=\"' + fromTx.toFixed(1) + '\" y=\"' + labY1 + '\" fill=\"#5f6368\" ' +\n"
    "                   'font-size=\"7.5\" font-family=\"sans-serif\">' + fromLbl + '</text>';\n"
    "            svg += '<text x=\"' + toTx.toFixed(1) + '\" y=\"' + labY2 + '\" fill=\"#1a73e8\" ' +\n"
    "                   'font-size=\"7.5\" font-family=\"sans-serif\" text-anchor=\"end\">' + toLbl + '</text>';"
)
c = apply("hyeto label stagger", old_labels, new_labels, c)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION C — Popup modal: add accumulation table + 15-min series tabs
# ═══════════════════════════════════════════════════════════════════════════════

# C1: Insert helper functions + global before updateSinglePopup
old_fn_boundary = "        function updateSinglePopup(id) {"
new_fn_boundary = (
    "        var _popStIds = {};\n"
    "\n"
    "        function generateAccumTable(id) {\n"
    "            var nowMs = slotToMs(endSlotIdx);\n"
    "            var periods = [1, 3, 6, 12, 24, 48];\n"
    "            var html = '<table style=\"width:100%;border-collapse:collapse;font-size:11px;line-height:1.7\">';\n"
    "            html += '<thead><tr>'\n"
    "                + '<th style=\"text-align:left;color:#5f6368;font-weight:500;padding:1px 0\">Period</th>'\n"
    "                + '<th style=\"text-align:right;color:#5f6368;font-weight:500;padding:1px 6px\">Total</th></tr></thead><tbody>';\n"
    "            periods.forEach(function(hrs) {\n"
    "                var val = computeAccum(id, nowMs, hrs);\n"
    "                html += '<tr>'\n"
    "                    + '<td style=\"padding:1px 0;color:#202124\">' + hrs + ' hr</td>'\n"
    "                    + '<td style=\"padding:1px 6px;text-align:right;font-weight:600;color:#202124\">' + val.toFixed(1) + ' mm</td></tr>';\n"
    "            });\n"
    "            html += '</tbody></table>';\n"
    "            return html;\n"
    "        }\n"
    "\n"
    "        function generateSeriesTable(id) {\n"
    "            var rows = [];\n"
    "            for (var i = endSlotIdx; i >= 0; i--) {\n"
    "                var t = new Date(slotToMs(i));\n"
    "                var dk = fmtDateKey(t);\n"
    "                var si = String(Math.round((t.getUTCHours() * 60 + t.getUTCMinutes()) / 15));\n"
    "                var day = dayCache[dk];\n"
    "                var v = (day && day.readings && day.readings[id] && day.readings[id][si] != null)\n"
    "                    ? +day.readings[id][si] : null;\n"
    "                if (v === null || v < 0) continue;\n"
    "                var ts = String(t.getHours()).padStart(2,'0') + ':' + String(t.getMinutes()).padStart(2,'0');\n"
    "                var ds = String(t.getDate()).padStart(2,'0') + ' ' + MONTHS[t.getMonth()];\n"
    "                rows.push('<tr>'\n"
    "                    + '<td style=\"padding:1px 0;color:#5f6368;white-space:nowrap\">' + ds + ' ' + ts + '</td>'\n"
    "                    + '<td style=\"padding:1px 6px;text-align:right;color:#202124\">' + v.toFixed(1) + '</td></tr>');\n"
    "            }\n"
    "            if (!rows.length) return '<div style=\"color:#9aa0a6;font-size:11px;padding:4px 0\">No data in window</div>';\n"
    "            return '<div style=\"max-height:130px;overflow-y:auto\">'\n"
    "                + '<table style=\"width:100%;border-collapse:collapse;font-size:11px;line-height:1.5\">'\n"
    "                + '<thead><tr>'\n"
    "                + '<th style=\"text-align:left;color:#5f6368;font-weight:500;padding:1px 0\">Time</th>'\n"
    "                + '<th style=\"text-align:right;color:#5f6368;font-weight:500;padding:1px 6px\">mm</th>'\n"
    "                + '</tr></thead><tbody>' + rows.join('') + '</tbody></table></div>';\n"
    "        }\n"
    "\n"
    "        function showPopTabFor(safeId, tab) {\n"
    "            var stId = _popStIds[safeId];\n"
    "            if (!stId) return;\n"
    "            var panel = document.getElementById('ppanel_' + safeId);\n"
    "            if (!panel) return;\n"
    "            panel.innerHTML = tab === 0 ? generateAccumTable(stId) : generateSeriesTable(stId);\n"
    "            [0, 1].forEach(function(i) {\n"
    "                var btn = document.getElementById('ptab' + i + '_' + safeId);\n"
    "                if (!btn) return;\n"
    "                btn.style.borderBottom = i === tab ? '2px solid #1a73e8' : '2px solid transparent';\n"
    "                btn.style.color = i === tab ? '#1a73e8' : '#5f6368';\n"
    "            });\n"
    "        }\n"
    "\n"
    "        function updateSinglePopup(id) {"
)
c = apply("insert popup helpers", old_fn_boundary, new_fn_boundary, c)

# C2: Modify updateSinglePopup to register safeId and add tabs
old_popup_end = (
    "            markers[id].setPopupContent(nameHtml + popCounty + popVal + hyetoHtml);\n"
    "        }"
)
new_popup_end = (
    "            var safeId = id.replace(/[^A-Za-z0-9]/g, '_');\n"
    "            _popStIds[safeId] = id;\n"
    "            var tabBtnStyle = 'background:none;border:none;border-bottom:2px solid transparent;padding:3px 8px;font-size:11px;cursor:pointer;font-family:inherit';\n"
    "            var tabsHtml = '<div style=\"margin-top:6px;border-top:1px solid #e8eaed;padding-top:4px\">'\n"
    "                + '<div style=\"display:flex;gap:0;margin-bottom:4px\">'\n"
    "                + '<button id=\"ptab0_' + safeId + '\" style=\"' + tabBtnStyle + ';border-bottom:2px solid #1a73e8;color:#1a73e8\"'\n"
    "                + ' onclick=\"showPopTabFor(\\'' + safeId + '\\',0)\">Totals</button>'\n"
    "                + '<button id=\"ptab1_' + safeId + '\" style=\"' + tabBtnStyle + ';color:#5f6368\"'\n"
    "                + ' onclick=\"showPopTabFor(\\'' + safeId + '\\',1)\">15-min</button>'\n"
    "                + '</div>'\n"
    "                + '<div id=\"ppanel_' + safeId + '\">' + generateAccumTable(id) + '</div>'\n"
    "                + '</div>';\n"
    "            markers[id].setPopupContent(nameHtml + popCounty + popVal + hyetoHtml + tabsHtml);\n"
    "        }"
)
c = apply("popup tabs", old_popup_end, new_popup_end, c)

with open(path, 'w', encoding='utf-8') as f:
    f.write(c)

print(f"\nDone. {len(c)-original_len:+d} chars. Fixes applied: {fixes}/13")
