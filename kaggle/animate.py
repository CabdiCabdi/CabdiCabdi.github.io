#!/usr/bin/env python3
"""
kaggle/animate.py
=================
Kaggle notebook script that animates a single static photo and uploads
the resulting MP4 to Cloudinary.

How it is triggered
-------------------
The Vercel /api/upload endpoint calls the Kaggle Kernels Push API and injects
these environment variables into the kernel run:

  INPUT_IMAGE_URL         – Cloudinary URL of the source photo
  JOB_ID                  – Unique hex string that identifies this job
  CLOUDINARY_CLOUD_NAME   – Cloudinary account identifier
  CLOUDINARY_API_KEY      – Cloudinary API key
  CLOUDINARY_API_SECRET   – Cloudinary API secret

Output
------
The animated MP4 is uploaded to Cloudinary with public_id:
  cabdi-animate/outputs/<JOB_ID>

The /api/status endpoint then looks up that public_id to retrieve the URL.

Animation approach
------------------
1.  Download the source image.
2.  Detect whether a human face is present (MediaPipe Face Detection).
3a. Portrait mode  – uses the LivePortrait pipeline (GPU accelerated) with a
    bundled talking-head driving video to produce realistic facial animation.
3b. General mode   – estimates depth with MiDaS and produces a parallax
    "Ken-Burns" zoom-and-shift effect that works for any subject.
4.  Encode with ffmpeg and upload the MP4 to Cloudinary.

Kaggle setup
------------
Add this script as "animate.py" to your Kaggle kernel and set:
  - Accelerator: GPU (P100 or T4)
  - Internet: ON  (to download models & upload result)

Install extras inside the notebook with:
  !pip install mediapipe cloudinary timm einops
"""

import os
import sys
import uuid
import hashlib
import hmac
import time
import json
import subprocess
import urllib.request
import urllib.parse
import base64
from pathlib import Path

# ─── Environment variables ────────────────────────────────────────────────────

INPUT_IMAGE_URL       = os.environ["INPUT_IMAGE_URL"]
JOB_ID                = os.environ["JOB_ID"]
CLOUDINARY_CLOUD_NAME = os.environ["CLOUDINARY_CLOUD_NAME"]
CLOUDINARY_API_KEY    = os.environ["CLOUDINARY_API_KEY"]
CLOUDINARY_API_SECRET = os.environ["CLOUDINARY_API_SECRET"]

# ─── Working directories ──────────────────────────────────────────────────────

WORK_DIR = Path("/kaggle/working")
INPUT_IMG = WORK_DIR / f"input_{JOB_ID}.jpg"
OUTPUT_MP4 = WORK_DIR / f"output_{JOB_ID}.mp4"

# ─── Utility ──────────────────────────────────────────────────────────────────

def run(cmd: str, check: bool = True):
    """Run a shell command and stream output."""
    print(f"\n$ {cmd}")
    result = subprocess.run(cmd, shell=True, text=True, capture_output=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed (exit {result.returncode}): {cmd}")
    return result


def download_file(url: str, dest: Path):
    print(f"Downloading {url} → {dest}")
    urllib.request.urlretrieve(url, dest)
    print(f"  Saved {dest.stat().st_size:,} bytes")


# ─── Step 1 – Download the source image ──────────────────────────────────────

print("=" * 60)
print("STEP 1 – Download input image")
print("=" * 60)

download_file(INPUT_IMAGE_URL, INPUT_IMG)

# ─── Step 2 – Detect face ────────────────────────────────────────────────────

print("\n" + "=" * 60)
print("STEP 2 – Detect face")
print("=" * 60)

run("pip install -q mediapipe")

import cv2
import numpy as np
import mediapipe as mp

face_img = cv2.imread(str(INPUT_IMG))
if face_img is None:
    raise ValueError(f"Could not read image: {INPUT_IMG}")

mp_face = mp.solutions.face_detection
has_face = False

with mp_face.FaceDetection(model_selection=1, min_detection_confidence=0.5) as detector:
    rgb = cv2.cvtColor(face_img, cv2.COLOR_BGR2RGB)
    results = detector.process(rgb)
    has_face = bool(results.detections)

print(f"Face detected: {has_face}")

# ─── Step 3 – Animate ────────────────────────────────────────────────────────

if has_face:
    # ── 3a  Portrait animation with LivePortrait ──────────────────────────────
    print("\n" + "=" * 60)
    print("STEP 3a – Portrait animation (LivePortrait)")
    print("=" * 60)

    # Install dependencies — try with default index first, fall back to cu118
    run("pip install -q torch torchvision", check=False)
    run("pip install -q einops timm")

    # Clone LivePortrait if not already present
    lp_dir = WORK_DIR / "LivePortrait"
    if not lp_dir.exists():
        run(f"git clone --depth 1 https://github.com/KwaiVGI/LivePortrait.git {lp_dir}")
        run(f"pip install -q -r {lp_dir}/requirements.txt")

    # Download pretrained weights (Hugging Face mirror on Kaggle or direct)
    weights_dir = lp_dir / "pretrained_weights"
    weights_dir.mkdir(parents=True, exist_ok=True)

    if not any(weights_dir.iterdir()):
        print("Downloading LivePortrait weights from Hugging Face…")
        run(
            "pip install -q huggingface_hub && "
            "python -c \""
            "from huggingface_hub import snapshot_download; "
            f"snapshot_download('KwaiVGI/LivePortrait', local_dir='{weights_dir}', "
            "ignore_patterns=['*.git*', 'README*'])"
            "\""
        )

    # Use the bundled driving video (expressions only, no head rotation)
    driving_video = lp_dir / "assets" / "examples" / "driving" / "d0.mp4"
    if not driving_video.exists():
        # Fallback: generate a simple neutral driving video
        print("Driving video not found – generating neutral placeholder…")
        driving_video = WORK_DIR / "driving_neutral.mp4"
        run(
            f"ffmpeg -y -f lavfi -i color=c=black:s=256x256:r=25 "
            f"-t 3 {driving_video}"
        )

    # Run LivePortrait inference
    run(
        f"cd {lp_dir} && python inference.py "
        f"--source_image {INPUT_IMG} "
        f"--driving_video {driving_video} "
        f"--output_dir {WORK_DIR} "
        f"--output_name output_{JOB_ID} "
        f"--flag_do_crop True "
        f"--flag_pasteback True"
    )

    # LivePortrait may produce the file with a different extension/path
    candidate = WORK_DIR / f"output_{JOB_ID}.mp4"
    if not candidate.exists():
        # Search for any mp4 produced in WORK_DIR
        candidates = sorted(WORK_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            candidate = candidates[0]
        else:
            raise FileNotFoundError("LivePortrait did not produce an MP4 output")

    if candidate != OUTPUT_MP4:
        candidate.rename(OUTPUT_MP4)

else:
    # ── 3b  General image – depth-based parallax animation ───────────────────
    print("\n" + "=" * 60)
    print("STEP 3b – Depth-based parallax animation")
    print("=" * 60)

    run("pip install -q torch torchvision timm", check=False)

    import torch

    # Load MiDaS for monocular depth estimation
    print("Loading MiDaS model…")
    midas = torch.hub.load("intel-isl/MiDaS", "MiDaS_small", trust_repo=True)
    midas.eval()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    midas.to(device)

    midas_transforms = torch.hub.load("intel-isl/MiDaS", "transforms", trust_repo=True)
    transform = midas_transforms.small_transform

    # Estimate depth
    img_bgr = cv2.imread(str(INPUT_IMG))
    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    h, w = img_bgr.shape[:2]

    input_batch = transform(img_rgb).to(device)
    with torch.no_grad():
        prediction = midas(input_batch)
        prediction = torch.nn.functional.interpolate(
            prediction.unsqueeze(1),
            size=img_bgr.shape[:2],
            mode="bicubic",
            align_corners=False,
        ).squeeze()
    depth = prediction.cpu().numpy()

    # Normalise depth to [0, 1]
    depth_min, depth_max = depth.min(), depth.max()
    if depth_max - depth_min > 1e-6:
        depth_norm = (depth - depth_min) / (depth_max - depth_min)
    else:
        depth_norm = np.zeros_like(depth)

    print(f"Depth map: min={depth_min:.3f}  max={depth_max:.3f}")

    # Generate a 3-second Ken-Burns parallax animation at 25 fps
    FPS       = 25
    DURATION  = 3          # seconds
    N_FRAMES  = FPS * DURATION
    MAX_SHIFT = min(w, h) * 0.04   # max pixel shift (≈4% of image size)

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    tmp_avi = WORK_DIR / f"tmp_{JOB_ID}.avi"
    writer  = cv2.VideoWriter(str(tmp_avi), fourcc, FPS, (w, h))

    # Pre-compute per-row mean depth once (shape: [h])
    row_mean_depth = depth_norm.mean(axis=1)

    # Base column index array (shape: [w])
    base_cols = np.arange(w, dtype=np.float32)

    for i in range(N_FRAMES):
        t = i / max(N_FRAMES - 1, 1)                       # 0 → 1
        angle = np.sin(t * 2 * np.pi) * MAX_SHIFT          # oscillating shift

        # Vectorised shift: src_cols[row, col] = col - depth[row] * angle
        # Shape: [h, w]
        shifts    = (row_mean_depth[:, np.newaxis] * angle).astype(np.float32)
        src_cols  = np.clip(base_cols[np.newaxis, :] - shifts, 0, w - 1).astype(np.int32)

        # Use advanced indexing to gather pixels: img_bgr[row, src_col]
        row_idx   = np.arange(h)[:, np.newaxis]             # [h, 1]
        shifted   = img_bgr[row_idx, src_cols]               # [h, w, 3]

        writer.write(shifted)

    writer.release()

    # Re-encode with ffmpeg to proper H.264 MP4
    run(
        f"ffmpeg -y -i {tmp_avi} "
        f"-vcodec libx264 -pix_fmt yuv420p "
        f"-preset fast -crf 22 {OUTPUT_MP4}"
    )
    tmp_avi.unlink(missing_ok=True)

# ─── Step 4 – Upload result to Cloudinary ────────────────────────────────────

print("\n" + "=" * 60)
print("STEP 4 – Upload result to Cloudinary")
print("=" * 60)


def cloudinary_upload_video(file_path: Path, job_id: str) -> str:
    """Upload an MP4 to Cloudinary and return its secure_url."""
    timestamp  = int(time.time())
    public_id  = f"cabdi-animate/outputs/{job_id}"

    params_to_sign = f"public_id={public_id}&timestamp={timestamp}"
    signature = hmac.new(
        CLOUDINARY_API_SECRET.encode(),
        params_to_sign.encode(),
        hashlib.sha1,
    ).hexdigest()

    boundary   = uuid.uuid4().hex
    file_bytes = file_path.read_bytes()

    body_parts_str = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="api_key"\r\n\r\n'
        f"{CLOUDINARY_API_KEY}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="timestamp"\r\n\r\n'
        f"{timestamp}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="signature"\r\n\r\n'
        f"{signature}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="public_id"\r\n\r\n'
        f"{public_id}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'
        f"Content-Type: video/mp4\r\n\r\n"
    )

    body_bytes = (
        body_parts_str.encode()
        + file_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )

    url = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/video/upload"
    req = urllib.request.Request(
        url,
        data=body_bytes,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())

    return result.get("secure_url") or result["url"]


video_url = cloudinary_upload_video(OUTPUT_MP4, JOB_ID)
print(f"\n✅  Animation uploaded successfully!")
print(f"    URL: {video_url}")

# Persist the URL to a local file so it can be read back if needed
(WORK_DIR / f"result_{JOB_ID}.txt").write_text(video_url)

print("\nDone.")
