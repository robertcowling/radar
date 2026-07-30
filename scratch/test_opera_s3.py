import boto3
from botocore import UNSIGNED
from botocore.client import Config
from datetime import datetime, timezone, timedelta

def test():
    # S3 Endpoint: https://s3.waw3-1.cloudferro.com/
    # Bucket: openradar-24h
    s3 = boto3.client(
        "s3",
        endpoint_url="https://s3.waw3-1.cloudferro.com/",
        region_name="us-east-1",  # dummy region
        config=Config(signature_version=UNSIGNED)
    )
    
    # Let's list files for today in OPERA/COMP
    now = datetime.now(timezone.utc)
    prefix = f"{now.strftime('%Y/%m/%d')}/OPERA/COMP/"
    print(f"Listing S3 bucket 'openradar-24h' with prefix: {prefix}...")
    
    try:
        resp = s3.list_objects_v2(Bucket="openradar-24h", Prefix=prefix)
        print("Response keys:", list(resp.keys()))
        if "Contents" in resp:
            contents = resp["Contents"]
            print(f"Found {len(contents)} files in S3.")
            # Print last 5 files
            print("Latest 5 files in S3:")
            for obj in contents[-5:]:
                print(f" - Key: {obj['Key']}, Size: {obj['Size']} bytes, LastModified: {obj['LastModified']}")
        else:
            print("No files found for today. Trying yesterday...")
            yesterday = now - timedelta(days=1)
            prefix_y = f"{yesterday.strftime('%Y/%m/%d')}/OPERA/COMP/"
            resp_y = s3.list_objects_v2(Bucket="openradar-24h", Prefix=prefix_y)
            if "Contents" in resp_y:
                contents = resp_y["Contents"]
                print(f"Found {len(contents)} files for yesterday.")
                print("Latest 5 files from yesterday:")
                for obj in contents[-5:]:
                    print(f" - Key: {obj['Key']}, Size: {obj['Size']} bytes")
            else:
                print("No files found for yesterday either.")
    except Exception as e:
        print("S3 Error:", e)

if __name__ == "__main__":
    test()
