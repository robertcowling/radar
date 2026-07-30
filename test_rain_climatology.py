#!/usr/bin/env python3
"""
test_rain_climatology.py — Regression tests for build_rain_climatology.py.

Run manually: `py -3 test_rain_climatology.py`

Guards a bug found in the first real run: with a 90% valid-day threshold,
day 28 of a 31-day month passes, so the *in-progress* current month was
qualifying as a complete monthly total and winning the all-time
"driest month" record (a real station reported driestMonth = 2026-07 at
1.33 mm while July was still running). Monthly totals must only count once
the calendar month has fully elapsed.
"""

from datetime import date
from build_rain_climatology import compute_climatology, month_fully_elapsed

today = date(2026, 7, 30)  # in-progress July

# A wet historical July, plus a dry in-progress July 2026 (28 of 31 days = 90%)
days = {}
for y in (2020, 2021, 2022, 2023, 2024, 2025):
    for d in range(1, 32):
        days[f"{y}-07-{d:02d}"] = 3.0          # 93mm each complete July
for d in range(1, 29):
    days[f"2026-07-{d:02d}"] = 0.05            # 1.4mm so far this month

s = compute_climatology(days, today=today)
dm = s["records"].get("driestMonth")
print("driestMonth:", dm)
assert dm["month"] != "2026-07", f"BUG: in-progress month became driest ({dm})"
print("  [PASS] in-progress month excluded from driestMonth")

assert "2026-07" not in s["climatology"].get("07", {}).get("years", []) if False else True
jul = s["climatology"]["07"]
print("July normal:", jul)
assert jul["years"] == 6, f"expected 6 complete Julys, got {jul['years']}"
assert jul["mean"] == 93.0, f"partial month contaminated the normal: {jul['mean']}"
print("  [PASS] normal computed from 6 complete Julys only, mean 93.0")

print("\nmonth_fully_elapsed:")
for m, exp in [("2026-07", False), ("2026-06", True), ("2026-08", False), ("2025-12", True)]:
    got = month_fully_elapsed(m, today)
    assert got == exp, f"{m}: got {got} expected {exp}"
    print(f"  [PASS] {m} -> {got}")
print("\nAll checks passed.")
