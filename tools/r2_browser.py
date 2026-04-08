"""
Cloudflare R2 Bucket Browser — local utility server.
Serves an HTML UI on http://localhost:2000 that lets you list, filter, sort,
preview, upload, download, and delete objects in the R2 bucket configured
via the same env vars used by process_latest.py.

Required env vars (set before launching, or in a .env alongside this file):
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
    R2_BUCKET_NAME, R2_PUBLIC_BASE_URL
"""

import http.server
import json
import os
import sys
import urllib.parse
import mimetypes
import io
import webbrowser
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Allow running from the tools/ directory or the repo root
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# ---------------------------------------------------------------------------
# Try loading a .env file next to this script (simple key=value, no quotes required)
# ---------------------------------------------------------------------------
def _load_dotenv():
    for candidate in [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        os.path.join(REPO_ROOT, ".env"),
    ]:
        if os.path.isfile(candidate):
            with open(candidate) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_dotenv()

import boto3

# ---------------------------------------------------------------------------
# R2 configuration
# ---------------------------------------------------------------------------
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET     = os.environ.get("R2_BUCKET_NAME", "")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")

def _check_config():
    missing = []
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME"):
        if not os.environ.get(name):
            missing.append(name)
    if missing:
        print(f"ERROR: Missing required env vars: {', '.join(missing)}")
        print("Set them in the environment or in a .env file next to this script.")
        sys.exit(1)

_check_config()

def get_r2():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )

# ---------------------------------------------------------------------------
# HTML UI (served inline)
# ---------------------------------------------------------------------------
HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "r2_browser.html")

def _read_html():
    with open(HTML_PATH, "r", encoding="utf-8") as f:
        return f.read()

# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------
class R2Handler(http.server.BaseHTTPRequestHandler):
    """Tiny JSON API + static HTML server."""

    def log_message(self, fmt, *args):
        # compact logging
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    # ----- helpers ----------------------------------------------------------
    def _json_response(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, msg):
        self._json_response({"error": msg}, status)

    # ----- routing ----------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self._serve_html()
        elif path == "/api/list":
            self._api_list(qs)
        elif path == "/api/info":
            self._api_info(qs)
        elif path == "/api/download":
            self._api_download(qs)
        elif path == "/api/presign":
            self._api_presign(qs)
        elif path == "/api/stats":
            self._api_stats()
        else:
            self._error(404, "not found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/delete":
            self._api_delete()
        elif path == "/api/upload":
            self._api_upload()
        elif path == "/api/rename":
            self._api_rename()
        elif path == "/api/delete-bulk":
            self._api_delete_bulk()
        else:
            self._error(404, "not found")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ----- static -----------------------------------------------------------
    def _serve_html(self):
        try:
            body = _read_html().encode("utf-8")
        except FileNotFoundError:
            self._error(500, "r2_browser.html not found next to r2_browser.py")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ----- API endpoints ----------------------------------------------------
    def _api_list(self, qs):
        prefix = qs.get("prefix", [""])[0]
        r2 = get_r2()
        objects = []
        paginator = r2.get_paginator("list_objects_v2")
        kwargs = {"Bucket": R2_BUCKET}
        if prefix:
            kwargs["Prefix"] = prefix
        for page in paginator.paginate(**kwargs):
            for obj in page.get("Contents", []):
                objects.append({
                    "key": obj["Key"],
                    "size": obj["Size"],
                    "modified": obj["LastModified"].isoformat(),
                    "etag": obj.get("ETag", "").strip('"'),
                })
        self._json_response({
            "bucket": R2_BUCKET,
            "public_url": R2_PUBLIC_URL,
            "count": len(objects),
            "objects": objects,
        })

    def _api_info(self, qs):
        key = qs.get("key", [""])[0]
        if not key:
            return self._error(400, "missing key param")
        r2 = get_r2()
        try:
            head = r2.head_object(Bucket=R2_BUCKET, Key=key)
            self._json_response({
                "key": key,
                "size": head["ContentLength"],
                "content_type": head.get("ContentType", ""),
                "modified": head["LastModified"].isoformat(),
                "etag": head.get("ETag", "").strip('"'),
                "metadata": head.get("Metadata", {}),
            })
        except Exception as e:
            self._error(404, str(e))

    def _api_download(self, qs):
        key = qs.get("key", [""])[0]
        if not key:
            return self._error(400, "missing key param")
        r2 = get_r2()
        try:
            resp = r2.get_object(Bucket=R2_BUCKET, Key=key)
            body = resp["Body"].read()
            ct = resp.get("ContentType", "application/octet-stream")
            filename = os.path.basename(key)
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self._error(404, str(e))

    def _api_presign(self, qs):
        key = qs.get("key", [""])[0]
        if not key:
            return self._error(400, "missing key param")
        if R2_PUBLIC_URL:
            self._json_response({"url": f"{R2_PUBLIC_URL}/{key}"})
        else:
            r2 = get_r2()
            url = r2.generate_presigned_url("get_object",
                Params={"Bucket": R2_BUCKET, "Key": key}, ExpiresIn=3600)
            self._json_response({"url": url})

    def _api_stats(self):
        r2 = get_r2()
        total_size = 0
        total_count = 0
        prefixes = {}
        paginator = r2.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=R2_BUCKET):
            for obj in page.get("Contents", []):
                total_count += 1
                total_size += obj["Size"]
                prefix = obj["Key"].split("/")[0] if "/" in obj["Key"] else "(root)"
                if prefix not in prefixes:
                    prefixes[prefix] = {"count": 0, "size": 0}
                prefixes[prefix]["count"] += 1
                prefixes[prefix]["size"] += obj["Size"]
        self._json_response({
            "bucket": R2_BUCKET,
            "total_count": total_count,
            "total_size": total_size,
            "prefixes": prefixes,
        })

    def _api_delete(self):
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length)) if length else {}
        key = data.get("key", "")
        if not key:
            return self._error(400, "missing key")
        r2 = get_r2()
        r2.delete_object(Bucket=R2_BUCKET, Key=key)
        self._json_response({"deleted": key})

    def _api_delete_bulk(self):
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length)) if length else {}
        keys = data.get("keys", [])
        if not keys:
            return self._error(400, "missing keys")
        r2 = get_r2()
        # R2 delete_objects max 1000 per call
        deleted = []
        for i in range(0, len(keys), 1000):
            batch = [{"Key": k} for k in keys[i:i+1000]]
            r2.delete_objects(Bucket=R2_BUCKET, Delete={"Objects": batch})
            deleted.extend(keys[i:i+1000])
        self._json_response({"deleted": deleted, "count": len(deleted)})

    def _api_upload(self):
        content_type_header = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type_header:
            return self._error(400, "expected multipart/form-data")

        # Extract boundary from Content-Type header
        boundary = ""
        for part in content_type_header.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[len("boundary="):]
                break
        if not boundary:
            return self._error(400, "no boundary in Content-Type")

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        # Parse multipart form data manually
        boundary_bytes = ("--" + boundary).encode()
        parts = body.split(boundary_bytes)
        # First part is empty (before first boundary), last is "--\r\n"

        dest_prefix = ""
        file_data = None
        filename = None

        for part in parts:
            if not part or part.strip() == b"--" or part.strip() == b"":
                continue
            # Split headers from body at \r\n\r\n
            if b"\r\n\r\n" not in part:
                continue
            header_block, payload = part.split(b"\r\n\r\n", 1)
            # Strip trailing \r\n from payload
            if payload.endswith(b"\r\n"):
                payload = payload[:-2]

            header_str = header_block.decode("utf-8", errors="replace")
            # Parse Content-Disposition
            disp = {}
            for header_line in header_str.split("\r\n"):
                if header_line.lower().startswith("content-disposition:"):
                    for token in header_line.split(";"):
                        token = token.strip()
                        if "=" in token:
                            k, v = token.split("=", 1)
                            disp[k.strip().lower()] = v.strip().strip('"')

            field_name = disp.get("name", "")
            if field_name == "prefix":
                dest_prefix = payload.decode("utf-8", errors="replace")
            elif field_name == "file":
                filename = disp.get("filename", "")
                file_data = payload

        if not filename or file_data is None:
            return self._error(400, "no file provided")

        filename = os.path.basename(filename)
        key = f"{dest_prefix}{filename}" if dest_prefix else filename
        ct = mimetypes.guess_type(filename)[0] or "application/octet-stream"

        r2 = get_r2()
        r2.put_object(Bucket=R2_BUCKET, Key=key, Body=file_data, ContentType=ct)
        self._json_response({"uploaded": key, "size": len(file_data)})

    def _api_rename(self):
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length)) if length else {}
        old_key = data.get("old_key", "")
        new_key = data.get("new_key", "")
        if not old_key or not new_key:
            return self._error(400, "missing old_key or new_key")
        r2 = get_r2()
        # copy then delete
        r2.copy_object(Bucket=R2_BUCKET, Key=new_key,
                       CopySource={"Bucket": R2_BUCKET, "Key": old_key})
        r2.delete_object(Bucket=R2_BUCKET, Key=old_key)
        self._json_response({"renamed": old_key, "to": new_key})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    server = http.server.HTTPServer(("127.0.0.1", port), R2Handler)
    print(f"R2 Browser running at http://localhost:{port}")
    print(f"Bucket: {R2_BUCKET}")
    print(f"Public URL: {R2_PUBLIC_URL or '(none)'}")
    print("Press Ctrl+C to stop.\n")
    webbrowser.open(f"http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()

if __name__ == "__main__":
    main()
