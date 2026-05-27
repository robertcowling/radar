import boto3, json, sys

r2 = boto3.client("s3",
    endpoint_url="https://495181df96d1d46ade158238a67fd89b.r2.cloudflarestorage.com",
    aws_access_key_id="e5a76e234c48aa43eac06611ef788ac8",
    aws_secret_access_key="90928614a301b79d5e6a229fa2e6306c9516fae45c85cf0d9b3b90f0c45b989a",
    region_name="auto")

obj = r2.get_object(Bucket="radarrainfall", Key="gaugecheck/results.json")
data = json.loads(obj["Body"].read())
print("generated_at:", data.get("generated_at"))
print("total_checked:", data.get("total_checked"))
print()

gauges = data.get("gauges", [])
if not gauges:
    print("No flagged gauges in results.")
    sys.exit(0)

for g in gauges:
    checks = g["checks"]
    nn    = checks.get("nearest_neighbour", {})
    drip  = checks.get("drip_leak", {})
    accum = checks.get("accum_anomaly", {})
    print(f"=== {g['name']} ({g['station_id']}) | {g['latest_value_mm']} mm | {g['checks_flag']} ===")
    print(f"  NN:    {nn}")
    print(f"  Drip:  {drip}")
    print(f"  Accum: {accum}")
    print(f"  LLM:   {g.get('llm_verdict')} — {g.get('llm_reasoning','')[:120]}")
    print()

# Also show a sample of non-flagged stations to verify NN check is running
print("--- Sample of non-flagged stations (NN check status) ---")
summary = data.get("summary", [])
flagged_ids = {g["station_id"] for g in gauges}
sample = [s for s in summary if s["id"] not in flagged_ids and s.get("mm", 0) > 1][:5]
print(f"(stations with mm>1 but not flagged — showing up to 5)")
for s in sample:
    print(f"  {s['n']} | {s['mm']} mm")
