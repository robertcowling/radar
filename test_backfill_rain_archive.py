#!/usr/bin/env python3
"""
test_backfill_rain_archive.py — Correctness tests for backfill_rain_archive.py.

Not part of the automated pipeline; run manually (`py -3 test_backfill_rain_archive.py`).
Exercises the EA Hydrology API item -> per-station daily total pipeline with
synthetic readings shaped exactly like real API responses, so a parsing
regression shows up as a wrong number rather than a silent no-op.
"""

import backfill_rain_archive as bra
from backfill_rain_archive import already_covered, daily_totals_from_items

FAILURES = []


def check(label, actual, expected):
    ok = actual == expected
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {actual!r}" + ("" if ok else f"  (expected {expected!r})"))
    if not ok:
        FAILURES.append(label)


def measure_uri(guid):
    return f"http://environment.data.gov.uk/hydrology/id/measures/{guid}-rainfall-t-900-mm-qualified"


def test_daily_totals_from_items():
    print("daily_totals_from_items()")
    suid_to_ref = {"guid-abc": "REF1"}
    items = [
        {"dateTime": "2025-05-01T00:00:00Z", "value": "0.2", "quality": "Good", "measure": measure_uri("guid-abc")},
        {"dateTime": "2025-05-01T00:15:00Z", "value": "0.3", "quality": "Good", "measure": measure_uri("guid-abc")},
        # negative value must be dropped (matches fetch_rain.parse_readings' val < 0 guard)
        {"dateTime": "2025-05-01T00:30:00Z", "value": "-1", "quality": "Good", "measure": measure_uri("guid-abc")},
        # "Missing" quality must be dropped
        {"dateTime": "2025-05-01T00:45:00Z", "value": "1.0", "quality": "Missing", "measure": measure_uri("guid-abc")},
        # GUID with no station mapping must be dropped, not crash
        {"dateTime": "2025-05-01T01:00:00Z", "value": "0.5", "quality": "Good", "measure": measure_uri("unknown-guid")},
        # a second real station to confirm multi-station grouping
        {"dateTime": "2025-05-01T01:15:00Z", "value": "4.4", "quality": "Good", "measure": measure_uri("guid-xyz")},
    ]
    suid_to_ref = {"guid-abc": "REF1", "guid-xyz": "REF2"}
    totals = daily_totals_from_items(items, suid_to_ref)
    check("known station sums only valid slots", totals.get("REF1"), 0.5)
    check("second station isolated correctly", totals.get("REF2"), 4.4)
    check("unmapped GUID produces no entry", "unknown-guid" in totals, False)
    check("exactly two stations resolved", len(totals), 2)


def test_daily_totals_empty():
    print("daily_totals_from_items() — no items")
    check("empty items -> empty totals", daily_totals_from_items([], {}), {})


def test_already_covered():
    print("already_covered()")
    archives = {"2025": {
        "A": {"2025-05-01": 1.0},
        "B": {"2025-05-01": 2.0},
    }}
    check("day with enough hits at low threshold -> covered", already_covered(archives, "2025-05-01", min_stations=2), True)
    check("day with too few hits -> not covered", already_covered(archives, "2025-05-01", min_stations=3), False)
    check("date absent everywhere -> not covered", already_covered(archives, "2025-05-02", min_stations=1), False)
    check("year absent from archives entirely -> not covered", already_covered({}, "2025-05-01", min_stations=1), False)


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {"items": []}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class FakeSession:
    """Returns a scripted sequence of responses/exceptions, one per .get() call."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url, timeout=None):
        self.calls += 1
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def test_fetch_day_rainfall_retries_on_429_then_succeeds(monkeypatch):
    print("fetch_day_rainfall() — 429 then success")
    slept = []
    monkeypatch.setattr(bra.time, "sleep", lambda s: slept.append(s))
    session = FakeSession([
        FakeResponse(429),
        FakeResponse(429),
        FakeResponse(200, {"items": [{"fake": "item"}]}),
    ])
    items = bra.fetch_day_rainfall(session, "2025-05-01")
    check("succeeds after two 429s", items, [{"fake": "item"}])
    check("made exactly 3 requests", session.calls, 3)
    check("backed off with increasing waits", slept, [20, 40])


def test_fetch_day_rainfall_gives_up_after_max_retries(monkeypatch):
    print("fetch_day_rainfall() — exhausts retries on persistent 429")
    monkeypatch.setattr(bra.time, "sleep", lambda s: None)
    session = FakeSession([FakeResponse(429)] * (bra.RATE_LIMIT_RETRIES + 1))
    items = bra.fetch_day_rainfall(session, "2025-05-01")
    check("returns [] rather than raising", items, [])
    check("made RATE_LIMIT_RETRIES + 1 attempts", session.calls, bra.RATE_LIMIT_RETRIES + 1)


def test_fetch_day_rainfall_404_short_circuits():
    print("fetch_day_rainfall() — 404 (date outside archive window)")
    session = FakeSession([FakeResponse(404)])
    items = bra.fetch_day_rainfall(session, "2020-01-01")
    check("404 returns [] with a single request, no retry", items, [])
    check("no retry attempted for 404", session.calls, 1)


def test_fetch_day_rainfall_network_error_returns_empty():
    print("fetch_day_rainfall() — connection error")
    session = FakeSession([ConnectionError("boom")])
    items = bra.fetch_day_rainfall(session, "2025-05-01")
    check("network exception swallowed, returns []", items, [])


class _MiniMonkeypatch:
    """Just enough of pytest's monkeypatch fixture for setattr/undo, since
    this repo has no test runner and these tests run as a plain script."""
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, value in reversed(self._undo):
            setattr(obj, name, value)


if __name__ == "__main__":
    test_daily_totals_from_items()
    test_daily_totals_empty()
    test_already_covered()

    mp = _MiniMonkeypatch()
    try:
        test_fetch_day_rainfall_retries_on_429_then_succeeds(mp)
        test_fetch_day_rainfall_gives_up_after_max_retries(mp)
    finally:
        mp.undo()
    test_fetch_day_rainfall_404_short_circuits()
    test_fetch_day_rainfall_network_error_returns_empty()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        raise SystemExit(1)
    print("All checks passed.")
