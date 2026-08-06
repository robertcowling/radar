"""
gcs_storms.py — minimal Google Cloud Storage read/write for storms.csv.

This is the only script in radar that touches GCS rather than Cloudflare R2:
storms.csv lives in the sibling floodforecast Flask app's GCS bucket, because
that app's own /storms page reads it from there directly. See radar's
CLAUDE.md ("Named storm archive") for the full "why" — the short version is
that this repo already had the openai secret and the workflow_dispatch
convention this pipeline needs, floodforecast didn't.

Auth is env-var-only (GOOGLE_JSON_KEY_FLOODFORECAST — a GCP service-account
JSON key, the *same* one floodforecast's own gcs_utils.py already uses under
this name, copied into a radar secret rather than a new key). No local-file or
bare-ADC fallback: those exist in floodforecast's version for a Replit
deployment, and neither is meaningful on a GitHub Actions runner. Missing the
env var should fail loudly rather than silently trying (and failing) some
other auth path.
"""

import json
import logging
import os
from io import StringIO

import pandas as pd
from google.cloud import storage
from google.oauth2 import service_account

logger = logging.getLogger(__name__)

BUCKET_NAME = "grounded-chain-373213.appspot.com"
STORMS_FILE = "storms.csv"


def get_storage_client():
    """A GCS client authenticated from GOOGLE_JSON_KEY_FLOODFORECAST.

    Raises rather than falling back to ADC — on a GitHub Actions runner there
    is no ambient credential a fallback could find, so a missing/malformed
    secret should surface immediately, not as a confusing downstream 403.
    """
    json_key_content = os.environ.get("GOOGLE_JSON_KEY_FLOODFORECAST", "").strip()
    if not json_key_content:
        raise RuntimeError(
            "GOOGLE_JSON_KEY_FLOODFORECAST is not set — add it as a radar repo "
            "secret (same GCP service-account JSON floodforecast's deployment uses)."
        )
    info = json.loads(json_key_content)
    credentials = service_account.Credentials.from_service_account_info(info)
    return storage.Client(credentials=credentials)


def read_csv_from_gcs(filename=STORMS_FILE):
    """Reads a CSV from the bucket into a DataFrame. Empty DataFrame if missing."""
    client = get_storage_client()
    bucket = client.bucket(BUCKET_NAME)
    blob = bucket.blob(filename)
    if not blob.exists():
        logger.warning("File %s not found in bucket %s.", filename, BUCKET_NAME)
        return pd.DataFrame()
    data = blob.download_as_text()
    return pd.read_csv(StringIO(data))


def write_csv_to_gcs(df, filename=STORMS_FILE):
    """Writes a DataFrame to the bucket as CSV."""
    client = get_storage_client()
    bucket = client.bucket(BUCKET_NAME)
    blob = bucket.blob(filename)
    blob.upload_from_string(df.to_csv(index=False), content_type="text/csv")
