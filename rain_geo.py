"""
rain_geo.py — Shared haversine distance + gauge neighbour index.

Centralizes what was previously three independent implementations
(rain/gauge.html's JS haversine, build_nrw_climatology.py's Python copy,
gauge_qc.py's own _haversine_km) into one, for new Python callers. The two
existing copies are left as-is (mechanical follow-up, not required for this
feature) — build_nrw_climatology.py's copy in particular stays put since it
has no import-order reason to change.
"""
import math


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two lat/lon points."""
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    c = 2 * math.asin(math.sqrt(a))
    return R * c


def build_neighbour_index(stations, k=10, radius_km=50):
    """stations: {id: {lat, lon, ...}} -> {id: [{"id":, "distKm":}, ...]}

    k-nearest within radius_km, sorted by distance. O(n^2) haversine pairs —
    fine for ~1,300 stations (~1.7M ops), not meant to be recomputed every
    15-min run; callers should cache the result (see rain/neighbours.json in
    make_rain_summary.py) and only rebuild when `stations` actually changes.
    """
    ids = [sid for sid, s in stations.items() if s.get("lat") is not None and s.get("lon") is not None]
    coords = {sid: (stations[sid]["lat"], stations[sid]["lon"]) for sid in ids}

    index = {}
    for i, sid in enumerate(ids):
        lat1, lon1 = coords[sid]
        candidates = []
        for other in ids:
            if other == sid:
                continue
            lat2, lon2 = coords[other]
            d = haversine_km(lat1, lon1, lat2, lon2)
            if d <= radius_km:
                candidates.append({"id": other, "distKm": round(d, 2)})
        candidates.sort(key=lambda c: c["distKm"])
        index[sid] = candidates[:k]
    return index


def stations_content_hash(stations):
    """Cheap change-detector for a neighbour-index rebuild gate — a hash of
    each station's id+lat+lon (order-independent), not the whole stations.json
    blob (name/county/etc changes shouldn't trigger a geometry rebuild)."""
    import hashlib
    parts = sorted(
        f"{sid}:{s.get('lat')}:{s.get('lon')}"
        for sid, s in stations.items()
        if s.get("lat") is not None and s.get("lon") is not None
    )
    return hashlib.md5("|".join(parts).encode()).hexdigest()
