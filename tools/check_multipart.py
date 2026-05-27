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
        print("Missing R2 credentials!")
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
        
    print(f"Listing uncompleted multipart uploads for R2 bucket '{bucket}'...")
    try:
        resp = r2.list_multipart_uploads(Bucket=bucket)
        uploads = resp.get('Uploads', [])
        if not uploads:
            print("No uncompleted multipart uploads found in the bucket.")
        else:
            print(f"Found {len(uploads)} uncompleted multipart uploads:")
            for upload in uploads:
                print(f"  Key: {upload['Key']} | ID: {upload['UploadId']} | Initiated: {upload['Initiated']}")
                # Optionally aborting
                print(f"  Aborting upload: {upload['Key']}...")
                r2.abort_multipart_upload(Bucket=bucket, Key=upload['Key'], UploadId=upload['UploadId'])
                print("    Aborted!")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
