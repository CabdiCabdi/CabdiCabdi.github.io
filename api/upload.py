"""
api/upload.py — Vercel serverless function
POST /api/upload

Accepts a multipart form-data upload with field name "photo".
1. Validates the file (type, size).
2. Uploads it to Cloudinary (temporary storage).
3. Triggers a Kaggle kernel run with the image URL as an environment variable.
4. Returns a JSON job_id that the frontend will poll.

Required environment variables (set in Vercel dashboard):
  CLOUDINARY_CLOUD_NAME
  CLOUDINARY_API_KEY
  CLOUDINARY_API_SECRET
  KAGGLE_USERNAME
  KAGGLE_KEY
  KAGGLE_KERNEL_SLUG   e.g.  "your-username/animate-photo"
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import re
import uuid
import base64
import hashlib
import hmac
import time
import urllib.request
import urllib.parse
import urllib.error


# ── Cloudinary helpers ───────────────────────────────────────────────────────

def cloudinary_upload(file_bytes: bytes, filename: str) -> dict:
    """Upload raw image bytes to Cloudinary and return the API response dict."""
    cloud_name  = os.environ["CLOUDINARY_CLOUD_NAME"]
    api_key     = os.environ["CLOUDINARY_API_KEY"]
    api_secret  = os.environ["CLOUDINARY_API_SECRET"]

    timestamp   = int(time.time())
    folder      = "cabdi-animate/inputs"
    public_id   = f"{folder}/{uuid.uuid4().hex}"

    # Generate SHA-1 signature
    params_to_sign = f"public_id={public_id}&timestamp={timestamp}"
    signature = hmac.new(
        api_secret.encode(),
        params_to_sign.encode(),
        hashlib.sha1,
    ).hexdigest()

    # Build multipart body manually (no external deps)
    boundary = uuid.uuid4().hex
    body_parts = []

    def add_field(name, value):
        body_parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        )

    add_field("api_key", api_key)
    add_field("timestamp", str(timestamp))
    add_field("signature", signature)
    add_field("public_id", public_id)

    # File field
    ext = os.path.splitext(filename)[1] or ".jpg"
    body_parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: image/{ext.lstrip('.')}\r\n\r\n"
    )

    body_bytes = (
        "".join(body_parts).encode()
        + file_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )

    url = f"https://api.cloudinary.com/v1_1/{cloud_name}/image/upload"
    req = urllib.request.Request(
        url,
        data=body_bytes,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


# ── Kaggle helper ────────────────────────────────────────────────────────────

def kaggle_run_kernel(image_url: str, job_id: str) -> dict:
    """
    Push a new run of the Kaggle kernel with the image URL injected as an
    environment variable.  Returns the Kaggle API response dict.
    """
    username    = os.environ["KAGGLE_USERNAME"]
    api_key     = os.environ["KAGGLE_KEY"]
    kernel_slug = os.environ["KAGGLE_KERNEL_SLUG"]   # e.g. "myuser/animate-photo"

    auth = base64.b64encode(f"{username}:{api_key}".encode()).decode()

    # Kaggle kernel push payload
    payload = json.dumps({
        "id": kernel_slug,
        "newTitle": "Animate Photo Run",
        "text": "",            # blank — kernel code lives on Kaggle
        "language": "python",
        "kernelType": "script",
        "isPrivate": True,
        "enableGpu": True,
        "enableInternet": True,
        "datasetDataSources": [],
        "kernelDataSources": [],
        "environmentVariables": [
            {"key": "INPUT_IMAGE_URL", "value": image_url},
            {"key": "JOB_ID",          "value": job_id},
            {"key": "CLOUDINARY_CLOUD_NAME", "value": os.environ["CLOUDINARY_CLOUD_NAME"]},
            {"key": "CLOUDINARY_API_KEY",    "value": os.environ["CLOUDINARY_API_KEY"]},
            {"key": "CLOUDINARY_API_SECRET", "value": os.environ["CLOUDINARY_API_SECRET"]},
        ],
    }).encode()

    url = "https://www.kaggle.com/api/v1/kernels/push"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {auth}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


# ── Vercel handler ───────────────────────────────────────────────────────────

ALLOWED_MIME  = {"image/jpeg", "image/png", "image/webp"}
MAX_FILE_SIZE = 10 * 1024 * 1024   # 10 MB

# MIME type inferred from filename extension when Content-Type is absent
_EXT_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".png": "image/png",  ".webp": "image/webp"}


def _parse_multipart(content_type: str, body: bytes) -> dict:
    """
    Minimal multipart/form-data parser.
    Returns a dict mapping field name → (filename | None, mime_type, bytes).
    """
    # Extract boundary
    m = re.search(r"boundary=([^\s;]+)", content_type)
    if not m:
        raise ValueError("No boundary found in Content-Type")
    boundary = m.group(1).strip('"').encode()

    parts = {}
    delimiter = b"--" + boundary
    raw_parts = body.split(delimiter)

    for part in raw_parts[1:]:          # skip preamble
        if part.strip() in (b"", b"--", b"--\r\n"):
            continue
        if part.startswith(b"--"):      # epilogue
            break

        # Split headers from body at first blank line
        if b"\r\n\r\n" in part:
            headers_raw, value = part.split(b"\r\n\r\n", 1)
        elif b"\n\n" in part:
            headers_raw, value = part.split(b"\n\n", 1)
        else:
            continue

        # Strip trailing CRLF before next boundary
        value = value.rstrip(b"\r\n")

        # Parse part headers
        headers: dict[str, str] = {}
        for line in headers_raw.split(b"\r\n"):
            line = line.decode(errors="replace")
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        disposition = headers.get("content-disposition", "")
        name_m    = re.search(r'name="([^"]*)"', disposition)
        fname_m   = re.search(r'filename="([^"]*)"', disposition)
        if not name_m:
            continue

        field_name = name_m.group(1)
        filename   = fname_m.group(1) if fname_m else None
        mime_type  = headers.get("content-type", "application/octet-stream").split(";")[0].strip()

        if filename and mime_type == "application/octet-stream":
            ext = os.path.splitext(filename)[1].lower()
            mime_type = _EXT_MIME.get(ext, mime_type)

        parts[field_name] = (filename, mime_type, value)

    return parts


class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        try:
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                return self._error(400, "Expected multipart/form-data")

            # Read the request body
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                return self._error(400, "Empty request body")
            if length > MAX_FILE_SIZE + 8192:   # extra headroom for headers
                return self._error(400, "Request body exceeds limit")

            body = self.rfile.read(length)

            # Parse the multipart body (no cgi / external deps)
            try:
                parts = _parse_multipart(content_type, body)
            except ValueError as exc:
                return self._error(400, f"Malformed multipart data: {exc}")

            if "photo" not in parts:
                return self._error(400, 'Missing "photo" field in form data')

            filename, mime_type, file_bytes = parts["photo"]
            filename = filename or "photo.jpg"

            if mime_type not in ALLOWED_MIME:
                return self._error(400, f"Unsupported file type: {mime_type}. Use JPG, PNG, or WEBP.")

            if len(file_bytes) > MAX_FILE_SIZE:
                return self._error(400, "File exceeds 10 MB limit")
            if len(file_bytes) == 0:
                return self._error(400, "Uploaded file is empty")

            # Upload to Cloudinary
            cloud_response = cloudinary_upload(file_bytes, filename)
            image_url      = cloud_response.get("secure_url") or cloud_response["url"]

            # Create a unique job ID for this run
            job_id = uuid.uuid4().hex

            # Trigger Kaggle kernel
            kaggle_response = kaggle_run_kernel(image_url, job_id)

            self._json(200, {
                "job_id":   job_id,
                "image_url": image_url,
                "kaggle":   kaggle_response,
            })

        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            self._error(502, f"Upstream API error ({exc.code}): {body[:300]}")
        except KeyError as exc:
            self._error(500, f"Missing environment variable: {exc}")
        except Exception as exc:
            self._error(500, str(exc))

    # ── helpers ──────────────────────────────────────────
    def _json(self, status: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str):
        self._json(status, {"error": message})

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
