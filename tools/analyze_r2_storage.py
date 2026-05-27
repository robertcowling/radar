import os
import boto3

def get_r2():
    env_vars = {}
    try:
        with open("tools/.env", "r") as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    env_vars[k.strip()] = v.strip()
    except Exception:
        pass

    account_id = os.environ.get("R2_ACCOUNT_ID") or env_vars.get("R2_ACCOUNT_ID", "")
    access_key_id = os.environ.get("R2_ACCESS_KEY_ID") or env_vars.get("R2_ACCESS_KEY_ID", "")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY") or env_vars.get("R2_SECRET_ACCESS_KEY", "")
    bucket = os.environ.get("R2_BUCKET_NAME") or env_vars.get("R2_BUCKET_NAME", "")
    
    if not all([account_id, access_key_id, secret_key, bucket]):
        print("Missing R2 credentials in environment variables or tools/.env!")
        return None, None
        
    r2 = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )
    return r2, bucket

def main():
    r2, bucket = get_r2()
    if not r2:
        return
        
    print(f"Listing and analyzing objects in R2 bucket '{bucket}'...")
    
    folders = {}  # prefix -> {'count': 0, 'size': 0}
    paginator = r2.get_paginator('list_objects_v2')
    
    total_objects = 0
    total_bytes = 0
    
    try:
        for page in paginator.paginate(Bucket=bucket):
            if 'Contents' not in page:
                continue
            for obj in page['Contents']:
                key = obj['Key']
                size = obj['Size']
                
                total_objects += 1
                total_bytes += size
                
                # Determine folder prefix
                parts = key.split('/')
                if len(parts) > 1:
                    prefix = parts[0] + '/'
                else:
                    prefix = 'root/'
                    
                if prefix not in folders:
                    folders[prefix] = {'count': 0, 'size': 0}
                folders[prefix]['count'] += 1
                folders[prefix]['size'] += size
                
        print("\n" + "="*60)
        print(f"{'Folder Prefix':<30} | {'File Count':<10} | {'Total Size':<15}")
        print("="*60)
        
        for prefix, info in sorted(folders.items(), key=lambda x: x[1]['size'], reverse=True):
            size_mb = info['size'] / (1024 * 1024)
            if size_mb > 1024:
                size_str = f"{size_mb / 1024:.2f} GB"
            else:
                size_str = f"{size_mb:.2f} MB"
            print(f"{prefix:<30} | {info['count']:<10} | {size_str:<15}")
            
        print("="*60)
        total_gb = total_bytes / (1024 * 1024 * 1024)
        print(f"Total Objects: {total_objects:,}")
        print(f"Total Storage: {total_gb:.2f} GB")
        print("="*60)
        
    except Exception as e:
        print(f"Error connecting or reading from R2: {e}")

if __name__ == "__main__":
    main()
