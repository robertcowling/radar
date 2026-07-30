import json

with open("rainfallsummarypolygon.json") as f:
    gj = json.load(f)

print("Type:", gj.get("type"))
print("Keys:", list(gj.keys()))
features = gj.get("features", [])
print("Number of features:", len(features))
if features:
    feat = features[0]
    print("Feature keys:", list(feat.keys()))
    print("Properties:", feat.get("properties"))
    geom = feat.get("geometry", {})
    print("Geometry type:", geom.get("type"))
    coords = geom.get("coordinates", [])
    if coords:
        if geom["type"] == "Polygon":
            print("First few coords:", coords[0][:5])
        elif geom["type"] == "MultiPolygon":
            print("First few coords of MultiPolygon:", coords[0][0][:5])
