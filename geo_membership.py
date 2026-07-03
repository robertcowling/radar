"""
geo_membership.py — Shared helpers for enriching flood-area reference data
with (a) a best-effort river/coastal-tidal/groundwater category, and (b) the
counties and rainfall-summary zones each area's polygon overlaps (a warning
can straddle several, so this returns lists, not a single value).

Used by fetch_ea_floodareas.py and build_nrw_floodareas.py.

Category is a FIRST-PASS HEURISTIC, not authoritative — see classify_category().
Neither the EA nor NRW static area APIs expose a clean river/coastal/
groundwater field. The live warning APIs *do* carry a genuine boolean
(EA: isTidal, NRW: TIDAL — confirmed against EA's API docs), which is a
reliable coastal/tidal signal and should be preferred over this heuristic
when available (see fetch_ea_warnings.py / fetch_nrw_warnings.py, which
overlay it onto the live snapshot). "Groundwater" has no structured flag
anywhere and is inferred purely from a keyword match on the area's own
name/description text.
"""
import json
import re

from shapely.geometry import shape
from shapely.strtree import STRtree

_GROUNDWATER_RE = re.compile(r"groundwater", re.IGNORECASE)
# Only match coastal keywords in the area *label* — descriptions often mention
# tidal limits / estuaries for river catchment areas that flow to the sea,
# which produces false positives (e.g. "River Teign Flood Watch Area").
_COASTAL_LABEL_RE = re.compile(r"\btidal\b|\bcoastal\b|\bcoast\b|\bestuary\b", re.IGNORECASE)
# A named sea body in the river/sea field is a reliable coastal indicator.
_SEA_BODY_RE = re.compile(
    r"\b(north sea|irish sea|english channel|bristol channel|"
    r"st george[''s]* channel|morecambe bay|cardigan bay|lyme bay|solent|"
    r"severn estuary|thames estuary|humber estuary)\b",
    re.IGNORECASE,
)


def classify_category(label, description="", river="", is_tidal=None):
    """Best-effort river / coastal / groundwater classification.

    is_tidal: pass the live API's isTidal/TIDAL boolean when known (takes
    priority over text heuristics). None when unknown (static area listing).

    Coastal detection is intentionally narrow — only the area *label* is
    checked for generic coastal keywords, not the description (descriptions
    routinely mention tidal limits for river catchments). Named sea bodies in
    the river field are also treated as coastal.
    """
    all_text = f"{label} {description} {river}"
    if is_tidal is True:
        return "coastal"
    if _GROUNDWATER_RE.search(all_text):
        return "groundwater"
    if is_tidal is False:
        return "river"
    if _COASTAL_LABEL_RE.search(label):
        return "coastal"
    if _SEA_BODY_RE.search(river):
        return "coastal"
    return "river"


class MembershipIndex:
    """Spatial index over a reference polygon layer (counties, rainfall
    zones, ...) for fast "which reference polygons does this area's polygon
    overlap" queries via shapely's STRtree, returning ALL overlaps (a
    warning area can straddle county/zone boundaries)."""

    def __init__(self, geojson_path, name_key):
        with open(geojson_path, encoding="utf-8") as f:
            data = json.load(f)
        self.names = []
        self.geoms = []
        for feat in data.get("features", []):
            name = feat.get("properties", {}).get(name_key)
            geom = feat.get("geometry")
            if not name or not geom:
                continue
            try:
                shp = shape(geom)
                if not shp.is_valid:
                    shp = shp.buffer(0)
            except Exception:
                continue
            self.names.append(name)
            self.geoms.append(shp)
        self.tree = STRtree(self.geoms)

    def overlaps(self, geom_dict):
        """Return the sorted list of reference-layer names whose polygon
        genuinely intersects the given GeoJSON geometry (not just bbox)."""
        try:
            shp = shape(geom_dict)
            if not shp.is_valid:
                shp = shp.buffer(0)
        except Exception:
            return []
        idxs = self.tree.query(shp)
        hits = []
        for i in idxs:
            ref = self.geoms[i]
            try:
                if shp.intersects(ref):
                    hits.append(self.names[i])
            except Exception:
                continue
        return sorted(set(hits))
