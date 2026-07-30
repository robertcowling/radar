from pyproj import Transformer

transformer = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)
# Dev & Cornwall Exmoor coord: [258176.058, 147606.787]
lon, lat = transformer.transform(258176.058, 147606.787)
print("Projected coords (lon, lat):", lon, lat)
