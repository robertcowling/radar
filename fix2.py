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
        idx = None
        # Try to find a nearby fragment for debugging
        frag = old.strip()[:60]
        i = text.find(frag)
        if i >= 0:
            print(f"    fragment found at {i}: {repr(text[i:i+100])}")
    else:
        print(f"  OK ({n}): {label}")
        fixes += n
    return text.replace(old, new)

# ── Fix 1: Remove "Until " from time display ─────────────────────────────────
c = apply("remove Until",
    "= 'Until ' + fmtLocal(validityMs)",
    "= fmtLocal(validityMs)",
    c)

# ── Fix 2: Replace scan-based threshold detection with current-state check ───
old_scan = (
    "            // Scan the 48-hr timeline; only include crossings whose full accumulation\n"
    "            // period lies within the visible window (no data from before slot 0)\n"
    "            var scanStart = slotToMs(0) + Math.round(accumHrs * 3600000);\n"
    "            var scanEnd   = slotToMs(totalSlots - 1);\n"
    "            var step = 3600000; // 1hr steps\n"
    "\n"
    "            var events = [];\n"
    "\n"
    "            if (thresholds.length) {\n"
    "                var prevState = {};\n"
    "                // Pre-seed prevState one step before scan to avoid spurious crossings\n"
    "                var initT = scanStart - step;\n"
    "                Object.keys(stations).forEach(function(id) {\n"
    "                    thresholds.forEach(function(thresh) {\n"
    "                        prevState[id + '|' + thresh.mm] = computeAccum(id, initT, accumHrs) >= thresh.mm;\n"
    "                    });\n"
    "                });\n"
    "                for (var t = scanStart; t <= scanEnd; t += step) {\n"
    "                    Object.keys(stations).forEach(function(id) {\n"
    "                        var val = computeAccum(id, t, accumHrs);\n"
    "                        thresholds.forEach(function(thresh) {\n"
    "                            var key = id + '|' + thresh.mm;\n"
    "                            var exceeds = val >= thresh.mm;\n"
    "                            if (exceeds && !prevState[key]) {\n"
    "                                // Only show crossings whose time falls in the highlighted window\n"
    "                                if (t >= slotToMs(startSlotIdx) && t <= slotToMs(endSlotIdx)) {\n"
    "                                    events.push({ms: t, id: id, val: val, thresh: thresh});\n"
    "                                }\n"
    "                            }\n"
    "                            prevState[key] = exceeds;\n"
    "                        });\n"
    "                    });\n"
    "                }\n"
    "                // Most recent first, then by amount\n"
    "                events.sort(function(a, b) { return b.ms !== a.ms ? b.ms - a.ms : b.val - a.val; });\n"
    "            }"
)
new_scan = (
    "            var events = [];\n"
    "\n"
    "            if (thresholds.length) {\n"
    "                var nowMs = slotToMs(endSlotIdx);\n"
    "                Object.keys(stations).forEach(function(id) {\n"
    "                    var val = computeAccum(id, nowMs, accumHrs);\n"
    "                    thresholds.forEach(function(thresh) {\n"
    "                        if (val >= thresh.mm) {\n"
    "                            events.push({ms: nowMs, id: id, val: val, thresh: thresh});\n"
    "                        }\n"
    "                    });\n"
    "                });\n"
    "                events.sort(function(a, b) { return b.val - a.val; });\n"
    "            }"
)
c = apply("threshold current-state check", old_scan, new_scan, c)

# ── Fix 3: Container 64→76px ─────────────────────────────────────────────────
c = apply("container 76px",
    "            height: 64px;",
    "            height: 76px;",
    c)

# ── Fix 4: Track top 70%→54% ─────────────────────────────────────────────────
c = apply("track top 54%",
    (
        "        .timeline-track {\n"
        "            position: absolute;\n"
        "            top: 70%;\n"
        "            left: 9px; right: 9px;"
    ),
    (
        "        .timeline-track {\n"
        "            position: absolute;\n"
        "            top: 54%;\n"
        "            left: 9px; right: 9px;"
    ),
    c)

# ── Fix 5: Ticks top 70%→54% ─────────────────────────────────────────────────
c = apply("ticks top 54%",
    (
        "        .timeline-ticks {\n"
        "            position: absolute;\n"
        "            top: 70%;\n"
        "            left: 8px; right: 8px;"
    ),
    (
        "        .timeline-ticks {\n"
        "            position: absolute;\n"
        "            top: 54%;\n"
        "            left: 8px; right: 8px;"
    ),
    c)

# ── Fix 6: Range-input top 70%→54% ───────────────────────────────────────────
c = apply("range-input top 54%",
    "            top: 70%; transform: translateY(-50%);",
    "            top: 54%; transform: translateY(-50%);",
    c)

# ── Fix 7: Tick-label top 20→18px ─────────────────────────────────────────────
c = apply("tick-label top 18px",
    (
        "        .tick-label {\n"
        "            position: absolute;\n"
        "            top: 20px;"
    ),
    (
        "        .tick-label {\n"
        "            position: absolute;\n"
        "            top: 18px;"
    ),
    c)

# ── Fix 8: Today-mark top -20→-30px ──────────────────────────────────────────
c = apply("today-mark top -30px",
    "            top: -20px;\n"
    "            font-size: 12px;",
    "            top: -30px;\n"
    "            font-size: 12px;",
    c)

# ── Fix 9: Grab bottom calc(100% + 14px)→calc(100% + 4px) ───────────────────
c = apply("grab bottom 4px",
    "            bottom: calc(100% + 14px);\n"
    "            left: 50%; margin-left: -14px;",
    "            bottom: calc(100% + 4px);\n"
    "            left: 50%; margin-left: -14px;",
    c)

with open(path, 'w', encoding='utf-8') as f:
    f.write(c)

print(f"\nDone. {len(c)-original_len:+d} chars. Fixes: {fixes}/9")
