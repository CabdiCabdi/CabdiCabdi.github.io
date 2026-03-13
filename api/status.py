"""
api/status.py — Vercel serverless function
GET /api/status?job_id=<id>

Checks the Kaggle kernel run status and, if complete, looks up the result
video URL from Cloudinary using the job_id tag.

Returns JSON:
  { "status": "queued|running|complete|error", "message": "...", "video_url": "..." }

Required environment variables (same as upload.py):
  KAGGLE_USERNAME
  KAGGLE_KEY
  KAGGLE_KERNEL_SLUG
  CLOUDINARY_CLOUD_NAME
  CLOUDINARY_API_KEY
  CLOUDINARY_API_SECRET
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import base64
import urllib.request
import urllib.parse
import urllib.error
import hashlib
import hmac
import time


# ── Kaggle helpers ───────────────────────────────────────────────────────────

def kaggle_auth_header() -> str:
    username = os.environ["KAGGLE_USERNAME"]
    api_key  = os.environ["KAGGLE_KEY"]
    return "Basic " + base64.b64encode(f"{username}:{api_key}".encode()).decode()


def kaggle_kernel_status() -> dict:
    """Return the latest run status for the configured kernel."""
    kernel_slug = os.environ["KAGGLE_KERNEL_SLUG"]  # "owner/kernel-name"
    owner, kernel_name = kernel_slug.split("/", 1)

    url = f"https://www.kaggle.com/api/v1/kernels/{owner}/{kernel_name}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": kaggle_auth_header()},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


# Kaggle status strings → our unified status
_KAGGLE_STATUS_MAP = {
    "queued":    "queued",
    "running":   "running",
    "complete":  "complete",
    "error":     "error",
    "cancelled": "error",
}


# ── Cloudinary helpers ───────────────────────────────────────────────────────

def cloudinary_find_video(job_id: str) -> str | None:
    """
    Search Cloudinary for the output video tagged with the job_id.
    The Kaggle script uploads the result with public_id:
      "cabdi-animate/outputs/<job_id>"
    Returns the secure_url or None if not found yet.
    """
    cloud_name = os.environ["CLOUDINARY_CLOUD_NAME"]
    api_key    = os.environ["CLOUDINARY_API_KEY"]
    api_secret = os.environ["CLOUDINARY_API_SECRET"]

    public_id = f"cabdi-animate/outputs/{job_id}"
    timestamp  = int(time.time())

    params_to_sign = f"public_id={public_id}&timestamp={timestamp}"
    signature = hmac.new(
        api_secret.encode(),
        params_to_sign.encode(),
        hashlib.sha1,
    ).hexdigest()

    # Use the Admin API to check if the resource exists
    url = (
        f"https://api.cloudinary.com/v1_1/{cloud_name}/resources/video/upload"
        f"?public_id={urllib.parse.quote(public_id, safe='')}"
        f"&timestamp={timestamp}&api_key={api_key}&signature={signature}"
    )

    auth = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Basic {auth}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            # data["resources"] is a list; if not empty, the video exists
            resources = data.get("resources", [])
            if resources:
                return resources[0].get("secure_url")
    except urllib.error.HTTPError:
        pass
    return None


# ── Vercel handler ───────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        try:
            # Parse query string
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            job_id_list = params.get("job_id", [])

            if not job_id_list:
                return self._error(400, "Missing job_id query parameter")

            job_id = job_id_list[0]
            if not job_id.isalnum():
                return self._error(400, "Invalid job_id")

            # Get Kaggle kernel status
            k_data  = kaggle_kernel_status()
            k_status = k_data.get("status", "").lower()
            status  = _KAGGLE_STATUS_MAP.get(k_status, "running")
            message = k_data.get("failureMessage") or k_data.get("statusMessage") or k_status

            video_url = None
            if status == "complete":
                video_url = cloudinary_find_video(job_id)
                if video_url is None:
                    # Kaggle says complete but video isn't uploaded yet — keep polling
                    status  = "running"
                    message = "Finalising video upload…"

            self._json(200, {
                "status":    status,
                "message":   message,
                "video_url": video_url,
            })

        except KeyError as exc:
            self._error(500, f"Missing environment variable: {exc}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            self._error(502, f"Kaggle API error ({exc.code}): {body[:300]}")
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
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
