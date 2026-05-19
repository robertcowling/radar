#!/usr/bin/env python3
import sys
sys.stdout.reconfigure(encoding='utf-8')

path = "rain/index.html"
with open(path, 'r', encoding='utf-8') as f:
    c = f.read()

original_len = len(c)
fixes = 0

# ── Fix 1: Revert HTML — put grab back inside timeline-track ────────────────
old_html = (
    '            <div class="range-grab" id="rangeGrab">\n'
    '                <span class="grab-line"></span>\n'
    '                <span class="grab-line"></span>\n'
    '                <span class="grab-line"></span>\n'
    '            </div>\n'
    '            <div class="timeline-track">\n'
    '                <div class="range-selected" id="rangeSelected"></div>\n'
    '            </div>'
)
new_html = (
    '            <div class="timeline-track">\n'
    '                <div class="range-selected" id="rangeSelected"></div>\n'
    '                <div class="range-grab" id="rangeGrab">\n'
    '                    <span class="grab-line"></span>\n'
    '                    <span class="grab-line"></span>\n'
    '                    <span class="grab-line"></span>\n'
    '                </div>\n'
    '            </div>'
)
n = c.count(old_html)
c = c.replace(old_html, new_html)
print(f"Fix 1 (HTML grab back inside track): {n}")
fixes += n

# ── Fix 2: Container 72→64px ─────────────────────────────────────────────────
old_h = "            height: 72px;"
new_h = "            height: 64px;"
n = c.count(old_h)
c = c.replace(old_h, new_h)
print(f"Fix 2 (container height 64px): {n}")
fixes += n

# ── Fix 3: Track 65%→70% ─────────────────────────────────────────────────────
for pct in ["65%"]:
    old_top = (
        "        .timeline-track {\n"
        "            position: absolute;\n"
        "            top: 65%;\n"
        "            left: 9px; right: 9px;"
    )
    new_top = (
        "        .timeline-track {\n"
        "            position: absolute;\n"
        "            top: 70%;\n"
        "            left: 9px; right: 9px;"
    )
    n = c.count(old_top)
    c = c.replace(old_top, new_top)
    print(f"Fix 3a (track top 70%): {n}")
    fixes += n

    old_ticks = (
        "        .timeline-ticks {\n"
        "            position: absolute;\n"
        "            top: 65%;\n"
        "            left: 8px; right: 8px;"
    )
    new_ticks = (
        "        .timeline-ticks {\n"
        "            position: absolute;\n"
        "            top: 70%;\n"
        "            left: 8px; right: 8px;"
    )
    n = c.count(old_ticks)
    c = c.replace(old_ticks, new_ticks)
    print(f"Fix 3b (ticks top 70%): {n}")
    fixes += n

    old_ri = "            top: 65%; transform: translateY(-50%);"
    new_ri = "            top: 70%; transform: translateY(-50%);"
    n = c.count(old_ri)
    c = c.replace(old_ri, new_ri)
    print(f"Fix 3c (range-input top 70%): {n}")
    fixes += n

# ── Fix 4: Grab CSS — back to bottom positioning, clear top/transform ────────
old_grab = (
    "        .range-grab {\n"
    "            position: absolute;\n"
    "            top: 4px;\n"
    "            transform: translateX(-50%);\n"
    "            width: 28px; height: 22px;"
)
new_grab = (
    "        .range-grab {\n"
    "            position: absolute;\n"
    "            bottom: calc(100% + 14px);\n"
    "            left: 50%; margin-left: -14px;\n"
    "            width: 28px; height: 22px;"
)
n = c.count(old_grab)
c = c.replace(old_grab, new_grab)
print(f"Fix 4 (grab CSS bottom): {n}")
fixes += n

# ── Fix 5: today-mark -17px → -20px (dates sit just below grab bottom) ───────
old_tm = "            top: -17px;"
new_tm = "            top: -20px;"
n = c.count(old_tm)
c = c.replace(old_tm, new_tm)
print(f"Fix 5 (today-mark top): {n}")
fixes += n

# ── Fix 6: Threshold events — only display if t is within current window ─────
old_event = (
    "            if (exceeds && !prevState[key]) {\n"
    "                                events.push({ms: t, id: id, val: val, thresh: thresh});\n"
    "                            }"
)
new_event = (
    "            if (exceeds && !prevState[key]) {\n"
    "                                // Only show crossings whose time falls in the highlighted window\n"
    "                                if (t >= slotToMs(startSlotIdx) && t <= slotToMs(endSlotIdx)) {\n"
    "                                    events.push({ms: t, id: id, val: val, thresh: thresh});\n"
    "                                }\n"
    "                            }"
)
n = c.count(old_event)
c = c.replace(old_event, new_event)
print(f"Fix 6 (filter events to window): {n}")
fixes += n

if n == 0:
    # Debug: find the actual push line
    idx = c.find("events.push({ms: t,")
    if idx >= 0:
        print(f"  events.push found at {idx}:")
        print(repr(c[max(0,idx-100):idx+100]))

with open(path, 'w', encoding='utf-8') as f:
    f.write(c)

print(f"\nDone. {len(c)-original_len:+d} chars. Fixes: {fixes}")
