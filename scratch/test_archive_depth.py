import urllib.request
import urllib.error
import json
import time
from datetime import datetime, timedelta

apikey = '6a52afd3ac0e491f265f331a52de29eb04a559787e8ff2777682f3ca88619bc8'

def check_date(date_obj):
    start = date_obj.strftime("%Y-%m-%dT00:00:00Z")
    end = (date_obj + timedelta(hours=23, minutes=59)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"https://api.meteogate.eu/warnings/collections/warnings/locations/UK?datetime={start}/{end}"
    req = urllib.request.Request(url, headers={'apikey': apikey})
    
    # Retry loop with exponential backoff on 429
    backoff = 1.0
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                body = res.read()
                if res.status == 200:
                    data = json.loads(body.decode('utf-8'))
                    features = data.get("features", [])
                    return 200, len(features)
                return res.status, 0
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # Rate limited, back off
                print(f"    [Rate limited 429 on {date_obj.strftime('%Y-%m-%d')}, waiting {backoff}s...]")
                time.sleep(backoff)
                backoff *= 2.0
                continue
            return e.code, e.reason
        except Exception as e:
            return str(e), 0
    return 429, "Rate limit retries exhausted"

# Test every day in February 1-15, 2025 with a mandatory sleep to respect rate limits
start_dt = datetime(2025, 2, 1)
print("Scanning February 1-15, 2025 day by day with rate-limit safety...")
days_with_warnings = 0
for day in range(15):
    test_dt = start_dt + timedelta(days=day)
    status, count = check_date(test_dt)
    # Add a polite sleep between requests
    time.sleep(0.5)
    
    if status == 200 and count > 0:
        print(f"  {test_dt.strftime('%Y-%m-%d')} -> 200 OK ({count} warnings)")
        days_with_warnings += 1
    elif status != 204:
        print(f"  {test_dt.strftime('%Y-%m-%d')} -> Status {status}")

print(f"Total days in December 2024 with warnings: {days_with_warnings}")
