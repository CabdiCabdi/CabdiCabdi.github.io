# CabdiAnimate

> AI-powered photo animation — upload a single static image and get back a smooth animated MP4.

**Live site:** [cabdi.me](https://cabdi.me)

---

## Architecture

```
Browser (cabdi.me)
  │
  │  POST /api/upload   (multipart photo)
  ▼
Vercel serverless (Python)
  ├── Uploads image to Cloudinary
  ├── Triggers Kaggle kernel via Kaggle API
  └── Returns { job_id }

  │  GET /api/status?job_id=…
  ▼
Vercel serverless (Python)
  ├── Polls Kaggle kernel status
  ├── When complete → looks up result video in Cloudinary
  └── Returns { status, video_url }

Kaggle GPU kernel (animate.py)
  ├── Downloads source image from Cloudinary
  ├── Detects face (MediaPipe)
  ├── Portrait? → LivePortrait neural animation
  │   Other?   → MiDaS depth-based parallax animation
  └── Uploads MP4 to Cloudinary (public_id: cabdi-animate/outputs/<job_id>)
```

---

## Quick-start

### 1 — Vercel environment variables

Set the following in your Vercel project settings (**Settings → Environment Variables**):

| Variable | Description |
|---|---|
| `CLOUDINARY_CLOUD_NAME` | Your Cloudinary cloud name |
| `CLOUDINARY_API_KEY` | Cloudinary API key |
| `CLOUDINARY_API_SECRET` | Cloudinary API secret |
| `KAGGLE_USERNAME` | Your Kaggle username |
| `KAGGLE_KEY` | Kaggle API key (from `~/.kaggle/kaggle.json`) |
| `KAGGLE_KERNEL_SLUG` | `your-username/animate-photo` |

### 2 — Cloudinary account

1. Sign up for a free account at [cloudinary.com](https://cloudinary.com).
2. Note your **Cloud Name**, **API Key**, and **API Secret** from the dashboard.
3. Enable **unsigned uploads** or use signed uploads (the backend uses signed uploads).

### 3 — Kaggle kernel setup

1. Create a new Kaggle notebook (Script type, not Notebook).
2. Paste the contents of [`kaggle/animate.py`](kaggle/animate.py) into the script.
3. Set **Accelerator → GPU** and **Internet → ON**.
4. Note the kernel slug: `your-kaggle-username/animate-photo`.
5. Add the `CLOUDINARY_*` secrets under **Settings → Environment Variables** in your Kaggle kernel.  
   *(These are injected again at runtime by the Vercel upload function.)*

### 4 — Deploy to Vercel

```bash
# Install Vercel CLI (if not already installed)
npm i -g vercel

# Deploy from the repo root
vercel --prod
```

Then connect your custom domain `cabdi.me` in the Vercel dashboard under **Domains**.

---

## Local development

```bash
# Install Vercel CLI
npm i -g vercel

# Run local dev server (serves both static files and /api routes)
vercel dev
```

The app is then available at `http://localhost:3000`.

---

## File structure

```
/
├── index.html          # Single-page frontend
├── css/style.css       # Styles
├── js/app.js           # Frontend logic (upload, polling, display)
├── api/
│   ├── upload.py       # Vercel: receive photo → Cloudinary → Kaggle
│   └── status.py       # Vercel: poll Kaggle status → return video URL
├── kaggle/
│   └── animate.py      # Kaggle GPU kernel: animate photo → upload MP4
├── vercel.json         # Vercel routing & build config
├── runtime.txt         # Python runtime version
├── CNAME               # Custom domain for GitHub Pages / Vercel
└── README.md
```

---

## Supported input formats

| Format | Extension |
|---|---|
| JPEG | `.jpg` / `.jpeg` |
| PNG  | `.png` |
| WebP | `.webp` |

Maximum file size: **10 MB**.

---

## Animation modes

| Input | Method | Notes |
|---|---|---|
| Portrait with face | [LivePortrait](https://github.com/KwaiVGI/LivePortrait) | Realistic facial animation driven by a neutral expression video |
| General image | MiDaS depth + parallax | Smooth Ken-Burns zoom/shift effect based on estimated depth |

---

## License

MIT