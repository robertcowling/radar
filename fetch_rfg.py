#!/usr/bin/env python3
"""
fetch_rfg.py — Rapid Flood Guidance (RFG) fetcher and R2 archiver.

Queries the public Met Office FFC API to get the latest Rapid Flood Guidance
status and document history. Downloads the latest RFG PDF and archives it
to Cloudflare R2 and local directories.
"""

import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone

# ── Cloudflare R2 Config ──────────────────────────────────────────────────────
R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
R2_PUBLIC_URL    = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET])

LATEST_KEY     = "rfg/rfg_latest.json"
ARCHIVE_PFX    = "rfg/archive"

# ── FFC API Config ────────────────────────────────────────────────────────────
# Default fallback to your active key
FFC_API_KEY = (
    os.environ.get("FGS_API_KEY", "").strip()
    or os.environ.get("METEOGATE_API_KEY", "").strip()
    or "4195a2d9d0a07d54be9178176db1f31b4d8553cdde736c11ec373b661ec520a3"
)
BASE_URL = "https://api.ffc-environment-agency.fgs.metoffice.gov.uk/api/public/v3/rfg"


def get_r2_client():
    if not USE_R2:
        return None
    try:
        import boto3
        return boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_KEY,
            region_name="auto",
        )
    except ImportError:
        print("boto3 not installed. Cloudflare R2 will be bypassed.")
        return None


def r2_put_json(r2, key, data):
    body = json.dumps(data, separators=(",", ":")).encode("utf-8")
    r2.put_object(
        Bucket=R2_BUCKET,
        Key=key,
        Body=body,
        ContentType="application/json; charset=utf-8"
    )


def r2_put_bytes(r2, key, body, content_type="application/pdf"):
    r2.put_object(
        Bucket=R2_BUCKET,
        Key=key,
        Body=body,
        ContentType=content_type
    )


def write_local_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))


def write_local_bytes(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(body)


def fetch_api_data(url):
    headers = {
        "X-Api-Key": FFC_API_KEY,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            if response.status != 200:
                print(f"  API returned status {response.status} for {url}")
                return None
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        print(f"  Error fetching {url}: {e}")
        return None


def download_pdf(url):
    print(f"  Downloading PDF from {url}...")
    headers = {
        "User-Agent": "Mozilla/5.0"
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            if response.status != 200:
                print(f"    Failed to download PDF: status {response.status}")
                return None
            return response.read()
    except Exception as e:
        print(f"    Error downloading PDF: {e}")
        return None


def main():
    now = datetime.now(timezone.utc)
    
    r2 = get_r2_client()
    if r2:
        print("Connected to Cloudflare R2 successfully.")
    else:
        print("Cloudflare R2 not configured. Operating in LOCAL ONLY / DRY RUN mode.")

    # 1. Fetch latest active RFG status
    print("Fetching latest active RFG status...")
    latest_active = fetch_api_data(f"{BASE_URL}/latest")
    if not latest_active:
        print("  Could not fetch RFG latest active status, aborting.")
        sys.exit(1)
    
    print(f"  Active Status: '{latest_active.get('status')}', Last Updated: {latest_active.get('last_updated')}")

    # 2. Fetch historical RFG updates to find the latest issued PDF and data
    print("Fetching historical RFGs...")
    history = fetch_api_data(BASE_URL)
    if not history:
        print("  Could not fetch RFG history, aborting.")
        sys.exit(1)
        
    rfg_updates = history.get("rfg_updates", [])
    print(f"  Found {len(rfg_updates)} historical RFG updates.")

    # Identify the latest issued RFG record (newest ID/date)
    latest_rfg = None
    if rfg_updates:
        # Sort history newest-first by ID
        rfg_updates.sort(key=lambda x: x.get("id", 0), reverse=True)
        latest_rfg = rfg_updates[0]
        print(f"  Latest issued RFG: ID {latest_rfg.get('id')} issued at {latest_rfg.get('issued_at')}")

    # 3. Cache the latest RFG PDF
    if latest_rfg and latest_rfg.get("pdf_url"):
        pdf_url = latest_rfg["pdf_url"]
        rfg_id = latest_rfg["id"]
        
        pdf_bytes = download_pdf(pdf_url)
        if pdf_bytes:
            local_pdf_path = f"rfg/archive/rfg_{rfg_id}.pdf"
            write_local_bytes(local_pdf_path, pdf_bytes)
            print(f"  Saved PDF locally to {local_pdf_path}")
            
            if r2:
                r2_pdf_key = f"{ARCHIVE_PFX}/rfg_{rfg_id}.pdf"
                try:
                    r2_put_bytes(r2, r2_pdf_key, pdf_bytes)
                    print(f"  Uploaded PDF to R2: {r2_pdf_key}")
                    # Rewrite the URL to point to our R2 custom domain
                    latest_rfg["original_pdf_url"] = pdf_url
                    latest_rfg["pdf_url"] = f"{R2_PUBLIC_URL}/{r2_pdf_key}"
                except Exception as e:
                    print(f"  Error uploading PDF to R2: {e}")
        else:
            print("  Warning: PDF download failed, using upstream PDF URL.")

    # 4. Save metadata JSON
    rfg_latest_data = {
        "generated_at": now.isoformat(),
        "status": latest_active.get("status"),
        "last_updated": latest_active.get("last_updated"),
        "latest_rfg": latest_rfg,
        "rfg_updates": rfg_updates[:10]  # Keep the 10 most recent updates in latest payload
    }

    # Local save
    write_local_json("rfg/rfg_latest.json", rfg_latest_data)
    print("Saved rfg/rfg_latest.json locally.")

    # R2 save
    if r2:
        try:
            r2_put_json(r2, LATEST_KEY, rfg_latest_data)
            print("Uploaded rfg_latest.json to Cloudflare R2.")
        except Exception as e:
            print(f"Error uploading rfg_latest.json to R2: {e}")

    print("RFG sync process completed successfully!")


if __name__ == "__main__":
    main()
