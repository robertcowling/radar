import json
from math import radians, sin, cos, sqrt, atan2

def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1))*cos(radians(lat2))*sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))

with open("rain/stations.json") as f:
    data = json.load(f)
stations = data["stations"]

nrw_numeric = {sid: s for sid, s in stations.items()
               if sid.startswith("nrw_") and sid[4:].isdigit()}
nrw_h = {sid: s for sid, s in stations.items()
         if sid.startswith("nrw_H")}

print(f"Numeric NRW (nrw_NNNN): {len(nrw_numeric)}")
print(f"H-prefix NRW (nrw_H...): {len(nrw_h)}")
print()

# For each numeric station, find nearest H station
orphans = []  # numeric stations with no nearby H partner
paired = []
DIST_KM = 3.0

for sid, s in sorted(nrw_numeric.items()):
    best_d, best_h, best_name = 999, None, None
    for hsid, hs in nrw_h.items():
        d = haversine(s["lat"], s["lon"], hs["lat"], hs["lon"])
        if d < best_d:
            best_d, best_h, best_name = d, hsid, hs["name"]
    if best_d <= DIST_KM:
        paired.append((sid, s["name"], best_h, best_name, best_d))
    else:
        orphans.append((sid, s["name"], s["lat"], s["lon"], best_d, best_name))

print(f"Paired (have nearby H station within {DIST_KM}km): {len(paired)}")
print(f"Orphans (no nearby H station): {len(orphans)}")
print()
if orphans:
    print("ORPHANS — would NOT be removed:")
    for sid, name, lat, lon, d, near in orphans:
        print(f"  {sid:20s} '{name}'  lat={lat:.3f} lon={lon:.3f}  (nearest H: {d:.1f}km '{near}')")
