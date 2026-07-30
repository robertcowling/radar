"""One-off: enrich the new UK-wide counties/UAs download with the
public_name/region/public_region fields the existing consumers expect,
deriving region via point-in-polygon against uk_regions.geojson (which
already covers Scotland and Northern Ireland).
"""
import json

SRC = r"C:/Users/rob_c/Downloads/uk-counties (2).geojson"
REGIONS = r"C:/Users/rob_c/Code/radar/uk_regions.geojson"
OUT = r"C:/Users/rob_c/Code/radar/scratch/uk-counties-new.geojson"


def _point_in_ring(lon, lat, ring):
    n, inside, j = len(ring), False, len(ring) - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _point_in_feature(lon, lat, feature):
    geom = feature["geometry"]
    if geom["type"] == "Polygon":
        return _point_in_ring(lon, lat, geom["coordinates"][0])
    if geom["type"] == "MultiPolygon":
        return any(_point_in_ring(lon, lat, poly[0]) for poly in geom["coordinates"])
    return False


def region_for(lon, lat, region_features):
    for feat in region_features:
        if _point_in_feature(lon, lat, feat):
            return feat["properties"].get("rgn19nm")
    return None


def main():
    with open(SRC, encoding="utf-8") as f:
        counties = json.load(f)
    with open(REGIONS, encoding="utf-8") as f:
        regions = json.load(f)["features"]

    unresolved = []
    for feat in counties["features"]:
        p = feat["properties"]
        name = p.get("name")
        p["public_name"] = name
        # England/Wales features already carry SNAC_GOR (the GOR region name);
        # Scotland/NI features lack it but have LAT/LONG, so derive region via
        # point-in-polygon against uk_regions.geojson (covers all 12 UK regions).
        region = p.get("SNAC_GOR") or None
        if not region and "LONG" in p and "LAT" in p:
            region = region_for(p["LONG"], p["LAT"], regions)
        if not region:
            unresolved.append(name)
        p["region"] = region or ""
        p["public_region"] = region or ""

    print(f"features: {len(counties['features'])}")
    print(f"unresolved region: {len(unresolved)} {unresolved}")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(counties, f)

    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
