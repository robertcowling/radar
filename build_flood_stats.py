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
    is_master = "type_raw" in df.columns
    if is_master:
        df = df.rename(columns={"date": "DATE", "area": "AREA", "code": "CODE",
                                "name": "WARNING / ALERT AREA NAME", "type_raw": "TYPE"})
    missing = [c for c in COLS if c not in df.columns]
    if missing:
        raise ValueError(f"{path.name}: missing columns {missing}")
    source_col = df["source"] if is_master and "source" in df.columns else None
    df = df[COLS].copy()
    if source_col is not None:
        # master file already carries the real per-row source; re-deriving it
        # from the filename here would stamp every row EA/NRW alike, since a
        # combined master's filename doesn't contain "nrw"
        df["source"] = source_col
    else:
        # raw quarterly file: NRW codes start 101/102/103 (Wales); also infer from filename
        is_nrw = "nrw" in path.name.lower()
        df["source"] = "NRW" if is_nrw else "EA"
    return df


def clean(frames: list[pd.DataFrame]) -> pd.DataFrame:
    df = pd.concat(frames, ignore_index=True)
    # format="mixed": inputs mix "YYYY-MM-DD HH:MM:SS" and "...SSS" (fractional
    # seconds) timestamps. Without it, pandas infers one format from the first
    # row and coerces every row that doesn't match it to NaT — silently
    # dropping ~10% of rows (confirmed against events_master.csv.gz).
    df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce", format="mixed")
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


def code_tier(code: str) -> str:
    """warning area vs alert area, from the code pattern"""
    if "FW" in code:
        return "FW"
    if "WA" in code:
        return "WA"
    return "?"


def build_aliases(df: pd.DataFrame, manual: dict | None = None) -> dict:
    """Map retired target-area codes to their current successor.

    EA/NRW have recoded areas over the years (e.g. Wessex 112FWF3C0A ->
    112FWFPAR10A in 2019). Detect these by: identical normalised name, same
    source, same tier (FW/WA), and near-sequential date ranges (<=365 days
    overlap). Manual overrides via aliases_manual.csv (old_code,new_code)
    take precedence and can also express merges the name match misses.
    """
    d = df.copy()
    d["nname"] = (d["name"].str.lower()
                  .str.replace(r"^\s*flood (watch|alert|warning)\s+", "", regex=True)
                  .str.replace(r"[^a-z0-9]", "", regex=True))
    spans = d.groupby("code").agg(first=("date", "min"), last=("date", "max"),
                                  src=("source", "first"))
    edges = []
    for _, grp in d.drop_duplicates(["nname", "code"]).groupby("nname"):
        codes = grp.code.unique()
        for i in range(len(codes)):
            for j in range(i + 1, len(codes)):
                a, b = codes[i], codes[j]
                if spans.loc[a, "src"] != spans.loc[b, "src"]:
                    continue
                if code_tier(a) != code_tier(b) or code_tier(a) == "?":
                    continue
                overlap = (min(spans.loc[a, "last"], spans.loc[b, "last"])
                           - max(spans.loc[a, "first"], spans.loc[b, "first"])).days
                if overlap <= 365:
                    edges.append((a, b))

    def components(nodes, edges):
        adj = {n: set() for n in nodes}
        for a, b in edges:
            if a in adj and b in adj:
                adj[a].add(b)
                adj[b].add(a)
        seen, comps = set(), []
        for n in nodes:
            if n in seen:
                continue
            stack, comp = [n], []
            while stack:
                x = stack.pop()
                if x in seen:
                    continue
                seen.add(x)
                comp.append(x)
                stack.extend(adj[x] - seen)
            comps.append(comp)
        return comps

    def overlap_days(a, b):
        return (min(spans.loc[a, "last"], spans.loc[b, "last"])
                - max(spans.loc[a, "first"], spans.loc[b, "first"])).days

    alias, ambiguous = {}, []

    def process(members):
        # validate: no two merged codes may coexist >1 year. If violated, eject
        # the worst offender to manual review and re-split the remainder along
        # the original name-link edges (so dependents follow their true link).
        if len(members) < 2:
            return
        viol = {c: 0 for c in members}
        ok = True
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                if overlap_days(members[i], members[j]) > 365:
                    viol[members[i]] += 1
                    viol[members[j]] += 1
                    ok = False
        if ok:
            canonical = max(members, key=lambda c: spans.loc[c, "last"])
            for c in members:
                if c != canonical:
                    alias[c] = canonical
            return
        worst = max(members, key=lambda c: (viol[c], -spans.loc[c, "last"].timestamp()))
        ambiguous.append(worst)
        rest = [c for c in members if c != worst]
        for sub in components(rest, edges):
            process(sub)

    all_nodes = sorted({c for e in edges for c in e})
    for comp in components(all_nodes, edges):
        process(comp)

    if manual:
        alias.update(manual)
        alias = {k: (alias.get(alias[k], alias[k])) for k in alias}
    return alias, sorted(set(ambiguous))


def freq_label(rate: float) -> str:
    for thr, lab in FREQ_LABELS:
        if rate >= thr:
            return lab
    return "Rare"


def build_area_stats(df: pd.DataFrame, alias: dict, current_areas: dict | None = None) -> dict:
    df = df.copy()
    df["orig_code"] = df["code"]
    df["code"] = df["code"].map(lambda c: alias.get(c, c))
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
            "merged_history": len(set(g["orig_code"])) > 1,
            "aliases": sorted(set(g["orig_code"]) - {code}),
            "code_history": [
                {"code": c, "from": h["date"].min().strftime("%Y-%m-%d"),
                 "to": h["date"].max().strftime("%Y-%m-%d"), "records": int(len(h))}
                for c, h in sorted(g.groupby("orig_code"),
                                   key=lambda kv: kv[1]["date"].min())
            ] if len(set(g["orig_code"])) > 1 else [],
            "former_names": sorted({n for n in g["name"].unique() if n != name}),
            "last_10": last10,
        }

    # live target areas with no recorded history since 2006 -> explicit zero entry
    if current_areas:
        for code, name in current_areas.items():
            if code not in out and alias.get(code, code) not in out:
                out[code] = {
                    "name": name, "source": "NRW" if code[:3] in ("101", "102", "103") else "EA",
                    "never_issued": True, "total_issued": 0, "counts": {},
                    "aliases": [], "last_10": [],
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
        "methodology": {
            "sources": "EA Historic Flood Warnings (quarterly open data) and NRW DS116342.",
            "code_merging": "Target areas recoded by EA/NRW over time are merged onto the "
                "current code where the same name, source and tier (FW/WA) apply and date "
                "ranges are sequential (<=1 year overlap, validated across every pair in a "
                "merge group). Merged records carry merged_history=true, full code_history "
                "with per-code date ranges, and former_names. Ambiguous cases are never "
                "auto-merged; they are listed in alias_review.csv for manual decision.",
            "rarity": "Frequency percentile ranks each area's issuances-per-year against "
                "all areas at the same severity tier. It describes how often warnings are "
                "issued relative to other areas, NOT hydrological return period or flood "
                "severity.",
            "never_issued": "Areas flagged never_issued are live target areas with no "
                "recorded issuance since the service began on 26 Jan 2006.",
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-o", "--outdir", default=".")
    ap.add_argument("--aliases", help="CSV of manual old_code,new_code overrides")
    ap.add_argument("--current-areas", help="CSV of live target areas (code,name) e.g. "
                    "from https://environment.data.gov.uk/flood-monitoring/id/floodAreas "
                    "- enables explicit 'never issued' entries and flags retired codes")
    args = ap.parse_args()

    frames = [load_file(Path(p)) for p in args.inputs]
    df = clean(frames)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    manual = None
    if args.aliases:
        m = pd.read_csv(args.aliases)
        manual = dict(zip(m.iloc[:, 0].astype(str).str.strip(),
                          m.iloc[:, 1].astype(str).str.strip()))
    alias, ambiguous = build_aliases(df, manual)
    pd.DataFrame(sorted(alias.items()), columns=["old_code", "current_code"]) \
        .to_csv(outdir / "code_aliases.csv", index=False)
    if ambiguous:
        pd.DataFrame({"code": ambiguous,
                      "reason": "excluded from auto-merge: overlaps another same-name area"}) \
            .to_csv(outdir / "merge_ambiguous.csv", index=False)

    current = None
    if args.current_areas:
        c = pd.read_csv(args.current_areas)
        current = dict(zip(c.iloc[:, 0].astype(str).str.strip(), c.iloc[:, 1].astype(str)))

    df.to_csv(outdir / "events_master.csv.gz", index=False, compression="gzip")
    area_stats = build_area_stats(df, alias, current)
    overall = build_overall(df.assign(code=df.code.map(lambda c: alias.get(c, c))), area_stats)

    (outdir / "area_stats.json").write_text(json.dumps(area_stats, separators=(",", ":")))
    (outdir / "overall_stats.json").write_text(json.dumps(overall, indent=1))

    print(f"{len(df):,} events, {len(area_stats):,} areas -> {outdir}")


if __name__ == "__main__":
    main()
