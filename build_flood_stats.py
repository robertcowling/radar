#!/usr/bin/env python3
"""
Build flood warning history + stats files for floodforecast.co.uk

Inputs:  any number of EA / NRW historic flood warning files (xlsx/ods/csv)
         with columns: DATE, AREA, CODE, WARNING / ALERT AREA NAME, TYPE
Outputs: events_master.csv.gz   canonical cleaned event log (rebuildable source of truth)
         area_stats.json        per target-area metadata for the viewer
         overall_stats.json     national headline stats

Update workflow:
  1. Drop new quarterly EA/NRW files (or a CSV export of live API events in the
     same column format) into the inputs list / --inputs args.
  2. Re-run this script. Everything is deduped on (CODE, DATE, TYPE), so
     overlapping files are safe.
  Or: append rows to events_master.csv.gz and re-run with only that file as input.

Usage:
  python build_flood_stats.py file1.xlsx file2.ods ... [-o outdir]
"""

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

COLS = ["DATE", "AREA", "CODE", "WARNING / ALERT AREA NAME", "TYPE"]

# canonical severity mapping (Flood Watch = pre-2010 name for Flood Alert)
SEVERITY = {
    "flood watch": ("Flood Alert", 1, False),
    "flood alert": ("Flood Alert", 1, False),
    "update flood alert": ("Flood Alert", 1, True),
    "flood warning": ("Flood Warning", 2, False),
    "update flood warning": ("Flood Warning", 2, True),
    "severe flood warning": ("Severe Flood Warning", 3, False),
    "update severe flood warning": ("Severe Flood Warning", 3, True),
}

FREQ_LABELS = [  # issuances per year (new issuances, not updates) -> label
    (6.0, "Very frequent"),
    (2.0, "Frequent"),
    (0.75, "Fairly common"),
    (0.25, "Occasional"),
    (0.0, "Rare"),
]


def load_file(path: Path) -> pd.DataFrame:
    if path.suffix == ".ods":
        df = pd.read_excel(path, engine="odf")
    elif path.suffix in (".xlsx", ".xlsm"):
        df = pd.read_excel(path)
    elif path.suffix in (".csv", ".gz"):
        df = pd.read_csv(path)
    else:
        raise ValueError(f"unsupported file type: {path}")
    # tolerate an already-cleaned master file as input
    if "type_raw" in df.columns:
        df = df.rename(columns={"date": "DATE", "area": "AREA", "code": "CODE",
                                "name": "WARNING / ALERT AREA NAME", "type_raw": "TYPE"})
    missing = [c for c in COLS if c not in df.columns]
    if missing:
        raise ValueError(f"{path.name}: missing columns {missing}")
    df = df[COLS].copy()
    # source: NRW codes start 101/102/103 (Wales); also infer from filename
    is_nrw = "nrw" in path.name.lower()
    df["source"] = "NRW" if is_nrw else "EA"
    return df


def clean(frames: list[pd.DataFrame]) -> pd.DataFrame:
    df = pd.concat(frames, ignore_index=True)
    df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce")
    df = df.dropna(subset=["DATE", "CODE", "TYPE"])
    key = df["TYPE"].str.strip().str.lower()
    mapped = key.map(SEVERITY)
    bad = df.loc[mapped.isna(), "TYPE"].unique()
    if len(bad):
        print(f"WARNING: unrecognised TYPE values dropped: {bad}", file=sys.stderr)
    df = df[mapped.notna()].copy()
    m = mapped.dropna()
    df["type"] = [t[0] for t in m]
    df["severity"] = [t[1] for t in m]
    df["is_update"] = [t[2] for t in m]
    df = df.rename(columns={"DATE": "date", "AREA": "area", "CODE": "code",
                            "WARNING / ALERT AREA NAME": "name", "TYPE": "type_raw"})
    df["code"] = df["code"].astype(str).str.strip()
    df["name"] = df["name"].astype(str).str.strip()
    df = df.drop_duplicates(subset=["code", "date", "type"])
    return df.sort_values("date").reset_index(drop=True)


def freq_label(rate: float) -> str:
    for thr, lab in FREQ_LABELS:
        if rate >= thr:
            return lab
    return "Rare"


def build_area_stats(df: pd.DataFrame) -> dict:
    ds_end = {s: df.loc[df.source == s, "date"].max() for s in df.source.unique()}
    issued = df[~df.is_update]

    # frequency percentile per severity tier, across all areas (for the
    # "how unusual" single value: 0 = rarest, 100 = most frequently issued area)
    rates = {}
    for sev in (1, 2, 3):
        sub = issued[issued.severity >= sev]
        if sub.empty:
            continue
        grp = sub.groupby("code").agg(n=("date", "size"), first=("date", "min"),
                                      src=("source", "first"))
        yrs = grp.apply(lambda r: max((ds_end[r["src"]] - r["first"]).days / 365.25, 1.0), axis=1)
        rate = grp["n"] / yrs
        pct = rate.rank(pct=True) * 100
        rates[sev] = (rate, pct)

    out = {}
    for code, g in df.groupby("code"):
        gi = g[~g.is_update]
        src = g["source"].iloc[0]
        name = g.sort_values("date")["name"].iloc[-1]  # most recent name
        first = gi["date"].min() if len(gi) else g["date"].min()
        last = gi["date"].max() if len(gi) else g["date"].max()
        years = max((ds_end[src] - first).days / 365.25, 1.0)

        counts = gi.groupby("type").size().to_dict()
        n_iss = len(gi)
        rate = n_iss / years

        # last 10 new issuances (most recent first)
        last10 = [
            {"d": r.date.strftime("%Y-%m-%dT%H:%M"), "t": r.type}
            for r in gi.sort_values("date", ascending=False).head(10).itertuples()
        ]

        # monthly climatology of issuances
        months = gi["date"].dt.month.value_counts().reindex(range(1, 13), fill_value=0)
        peak_month = int(months.idxmax()) if n_iss else None

        # median gap between issuances
        gaps = gi["date"].sort_values().diff().dt.days.dropna()
        median_gap = int(gaps.median()) if len(gaps) else None

        # highest severity ever seen at this area + rarity for that tier
        max_sev = int(g["severity"].max())

        def tier(sev):
            if sev not in rates or code not in rates[sev][0].index:
                return None
            r = rates[sev][0][code]
            return {
                "per_year": round(float(r), 2),
                "avg_days_between": int(365.25 / r) if r > 0 else None,
                "percentile": round(float(rates[sev][1][code]), 1),
                "label": freq_label(r),
            }

        out[code] = {
            "name": name,
            "source": src,
            "ea_area": g.sort_values("date")["area"].iloc[-1],
            "first_issued": first.strftime("%Y-%m-%d"),
            "last_issued": last.strftime("%Y-%m-%d"),
            "total_issued": n_iss,
            "counts": counts,
            "per_year": round(rate, 2),
            "median_gap_days": median_gap,
            "monthly": months.tolist(),
            "peak_month": peak_month,
            "max_severity": max_sev,
            "freq": {str(s): tier(s) for s in (1, 2, 3) if tier(s)},
            "last_10": last10,
        }
    return out


def build_overall(df: pd.DataFrame, area_stats: dict) -> dict:
    issued = df[~df.is_update]
    by_year = issued.groupby(issued.date.dt.year).size()
    by_month = issued.groupby(issued.date.dt.month).size().reindex(range(1, 13), fill_value=0)

    def top(sev, n=20):
        sub = issued[issued.severity == sev]
        t = sub.groupby("code").size().nlargest(n)
        return [{"code": c, "name": area_stats[c]["name"], "source": area_stats[c]["source"],
                 "count": int(v)} for c, v in t.items()]

    severe = issued[issued.severity == 3].sort_values("date", ascending=False).head(20)
    return {
        "generated": pd.Timestamp.now().strftime("%Y-%m-%d"),
        "coverage": {
            "start": issued.date.min().strftime("%Y-%m-%d"),
            "end": issued.date.max().strftime("%Y-%m-%d"),
            "ea_end": issued.loc[issued.source == "EA", "date"].max().strftime("%Y-%m-%d"),
            "nrw_end": issued.loc[issued.source == "NRW", "date"].max().strftime("%Y-%m-%d"),
        },
        "totals": {
            "issuances": int(len(issued)),
            "by_type": issued.groupby("type").size().to_dict(),
            "by_source": issued.groupby("source").size().to_dict(),
            "areas": len(area_stats),
        },
        "busiest_years": {int(k): int(v) for k, v in by_year.nlargest(10).items()},
        "by_year": {int(k): int(v) for k, v in by_year.items()},
        "by_month": by_month.tolist(),
        "top_alert_areas": top(1),
        "top_warning_areas": top(2),
        "top_severe_areas": top(3, 10),
        "recent_severe_warnings": [
            {"d": r.date.strftime("%Y-%m-%d"), "code": r.code, "name": r.name}
            for r in severe.itertuples()
        ],
        "notes": [
            "Flood Watch (pre-2010) counted as Flood Alert.",
            "Update messages excluded from issuance counts.",
            "NRW data gap May-Aug 2024 between source datasets.",
            "Rates use each area's first issuance to dataset end; recently created areas may look busier.",
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-o", "--outdir", default=".")
    args = ap.parse_args()

    frames = [load_file(Path(p)) for p in args.inputs]
    df = clean(frames)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    df.to_csv(outdir / "events_master.csv.gz", index=False, compression="gzip")
    area_stats = build_area_stats(df)
    overall = build_overall(df, area_stats)

    (outdir / "area_stats.json").write_text(json.dumps(area_stats, separators=(",", ":")))
    (outdir / "overall_stats.json").write_text(json.dumps(overall, indent=1))

    print(f"{len(df):,} events, {len(area_stats):,} areas -> {outdir}")


if __name__ == "__main__":
    main()
