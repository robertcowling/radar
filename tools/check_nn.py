import boto3, json, os, sys

R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
BUCKET           = os.environ.get("R2_BUCKET_NAME", "")

if not all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, BUCKET]):
    sys.exit("Missing R2 credentials — set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
              "R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME in the environment.")

r2 = boto3.client("s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_KEY,
    region_name="auto")

obj = r2.get_object(Bucket=BUCKET, Key="gaugecheck/results.json")
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
