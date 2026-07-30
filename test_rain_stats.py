#!/usr/bin/env python3
"""
test_rain_stats.py — Correctness tests for make_rain_summary.py's stats logic.

Not part of the automated pipeline; run manually (`py -3 test_rain_stats.py`)
whenever compute_records()/threshold_level() change. Uses hand-computed
expected values against synthetic archives so failures point at the actual
arithmetic bug rather than "output looks different from last time".
"""

from datetime import datetime, timezone

from make_rain_summary import compute_records, threshold_level

FAILURES = []


def check(label, actual, expected):
    ok = actual == expected
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {actual!r}" + ("" if ok else f"  (expected {expected!r})"))
    if not ok:
        FAILURES.append(label)


def test_compute_records():
    print("compute_records()")
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    archives = {
        2025: {
            "TEST1": {
                "2025-04-08": 5.0,
                "2025-04-09": 0.0,
                "2025-04-10": 0.5,
                "2025-04-11": 0.2,
                "2025-04-12": 3.0,
                # gap: 04-13, 04-14 missing (gauge offline) — must NOT bridge the dry run
                "2025-04-15": 0.0,
                "2025-04-16": 0.1,
                "2025-04-17": 0.1,
                "2025-05-01": 20.0,
            },
        },
        2026: {
            "TEST1": {
                "2026-06-01": 100.0,
                "2026-06-02": 5.0,
                "2026-06-15": 61.9,
                "2026-07-01": 10.6,   # current month — must be excluded from driest/wettest month
                "2026-07-02": 0.0,
            },
        },
    }
    r = compute_records({"TEST1": {}}, archives, now)["TEST1"]

    check("monthTotal (July 2026 so far)", r["monthTotal"], 10.6)
    check("yearTotal (2026)", r["yearTotal"], 177.5)
    check("wettestDay", r["wettestDay"], {"date": "2026-06-01", "mm": 100.0})
    check("wettestMonth (excludes current month)", r["wettestMonth"], {"month": "2026-06", "mm": 166.9})
    check("driestMonth (excludes current month)", r["driestMonth"], {"month": "2025-04", "mm": 8.9})
    check("longestDrySpell (gap must not bridge)", r["longestDrySpell"], {"days": 3, "from": "2025-04-09"})
    check("recordsSince", r["recordsSince"], "2025-04-08")


def test_compute_records_no_data():
    print("compute_records() — station with no archive entries")
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    r = compute_records({"EMPTY": {}}, {2026: {}}, now)["EMPTY"]
    check("monthTotal", r["monthTotal"], 0.0)
    check("yearTotal", r["yearTotal"], 0.0)
    check("wettestDay", r["wettestDay"], None)
    check("wettestMonth", r["wettestMonth"], None)
    check("driestMonth", r["driestMonth"], None)
    check("longestDrySpell", r["longestDrySpell"], None)
    check("recordsSince", r["recordsSince"], None)


def test_compute_records_only_current_month():
    print("compute_records() — only current-month data (no complete months yet)")
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    archives = {2026: {"NEW1": {"2026-07-05": 4.0, "2026-07-06": 6.0}}}
    r = compute_records({"NEW1": {}}, archives, now)["NEW1"]
    check("monthTotal falls back to current month when it's all there is", r["monthTotal"], 10.0)
    check("wettestMonth falls back to current (incomplete) month", r["wettestMonth"], {"month": "2026-07", "mm": 10.0})
    check("driestMonth falls back to current (incomplete) month", r["driestMonth"], {"month": "2026-07", "mm": 10.0})


def test_dry_spell_continues_across_month_boundary():
    print("compute_records() — dry spell spanning a month boundary")
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    archives = {2026: {"SPAN1": {
        "2026-05-30": 0.0, "2026-05-31": 0.0, "2026-06-01": 0.0, "2026-06-02": 0.0,
        "2026-06-03": 5.0,
    }}}
    r = compute_records({"SPAN1": {}}, archives, now)["SPAN1"]
    check("dry spell spans month boundary (consecutive calendar days)", r["longestDrySpell"], {"days": 4, "from": "2026-05-30"})


def test_threshold_level():
    print("threshold_level()")
    check("well below all bands -> normal", threshold_level({"last1h": 5, "last3h": 10, "last6h": 15}), "normal")
    check("1h heavy (>10, <=30)", threshold_level({"last1h": 15, "last3h": 10, "last6h": 15}), "heavy")
    check("1h extreme (>30)", threshold_level({"last1h": 35, "last3h": 10, "last6h": 15}), "extreme")
    check("3h extreme dominates despite normal 1h", threshold_level({"last1h": 5, "last3h": 45, "last6h": 15}), "extreme")
    check("3h heavy only", threshold_level({"last1h": 5, "last3h": 25, "last6h": 15}), "heavy")
    check("6h extreme boundary (exactly 50 is heavy, not extreme — > required)", threshold_level({"last1h": 0, "last3h": 0, "last6h": 50}), "heavy")
    check("6h extreme just above boundary", threshold_level({"last1h": 0, "last3h": 0, "last6h": 50.1}), "extreme")
    check("missing keys default to 0 -> normal", threshold_level({}), "normal")


if __name__ == "__main__":
    test_compute_records()
    test_compute_records_no_data()
    test_compute_records_only_current_month()
    test_dry_spell_continues_across_month_boundary()
    test_threshold_level()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        raise SystemExit(1)
    print("All checks passed.")
