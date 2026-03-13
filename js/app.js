/* ═══════════════════════════════════════════════════════
   CabdiAnimate — app.js
   Handles: file selection, upload, status polling,
            video display and download.
   ═══════════════════════════════════════════════════════ */

'use strict';

// ── Config ──────────────────────────────────────────────
const API_BASE          = '/api';          // Vercel serverless functions
const POLL_INTERVAL_MIN = 5_000;           // ms — initial poll interval
const POLL_INTERVAL_MAX = 20_000;          // ms — max poll interval (exponential backoff)
const MAX_POLL_TIME     = 5 * 60_000;      // 5 minute hard timeout
const MAX_FILE_SIZE     = 10 * 1024 * 1024; // 10 MB

// ── DOM references ───────────────────────────────────────
const stepUpload     = document.getElementById('step-upload');
const stepProcessing = document.getElementById('step-processing');
const stepResult     = document.getElementById('step-result');

const dropZone       = document.getElementById('drop-zone');
const fileInput      = document.getElementById('file-input');
const previewArea    = document.getElementById('preview-area');
const previewImg     = document.getElementById('preview-img');
const btnChange      = document.getElementById('btn-change');
const btnUpload      = document.getElementById('btn-upload');
const uploadError    = document.getElementById('upload-error');

const progressLog    = document.getElementById('progress-log');
const processingErr  = document.getElementById('processing-error');

const resultVideo    = document.getElementById('result-video');
const btnDownload    = document.getElementById('btn-download');
const btnRestart     = document.getElementById('btn-restart');

// ── State ────────────────────────────────────────────────
let selectedFile  = null;
let pollTimer     = null;
let pollStartTime = 0;
let pollInterval  = POLL_INTERVAL_MIN;

// ── Helpers ──────────────────────────────────────────────
function showStep(step) {
  [stepUpload, stepProcessing, stepResult].forEach(s => s.classList.add('hidden'));
  step.classList.remove('hidden');
  step.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function setUploadError(msg) {
  uploadError.textContent = msg;
  uploadError.classList.toggle('hidden', !msg);
}

function setProcessingError(msg) {
  processingErr.textContent = msg;
  processingErr.classList.toggle('hidden', !msg);
}

function addLog(msg, type = '') {
  const span = document.createElement('span');
  span.className = 'log-entry' + (type ? ' ' + type : '');
  span.textContent = new Date().toLocaleTimeString() + '  ' + msg;
  progressLog.appendChild(span);
  progressLog.appendChild(document.createElement('br'));
  progressLog.scrollTop = progressLog.scrollHeight;
}

function resetAll() {
  stopPolling();
  selectedFile  = null;
  pollInterval  = POLL_INTERVAL_MIN;
  previewArea.classList.add('hidden');
  dropZone.classList.remove('hidden');
  btnUpload.classList.add('hidden');
  btnUpload.disabled = true;
  setUploadError('');
  progressLog.innerHTML = '';
  setProcessingError('');
  resultVideo.src = '';
  fileInput.value = '';
  showStep(stepUpload);
}

// ── File validation & preview ────────────────────────────
function handleFile(file) {
  if (!file) return;

  if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) {
    setUploadError('Please select a JPG, PNG, or WEBP image.');
    return;
  }
  if (file.size > MAX_FILE_SIZE) {
    setUploadError('File is too large. Maximum size is 10 MB.');
    return;
  }

  setUploadError('');
  selectedFile = file;

  const reader = new FileReader();
  reader.onload = e => {
    previewImg.src = e.target.result;
    previewArea.classList.remove('hidden');
    dropZone.classList.add('hidden');
    btnUpload.classList.remove('hidden');
    btnUpload.disabled = false;
  };
  reader.readAsDataURL(file);
}

// ── Drag & Drop ──────────────────────────────────────────
dropZone.addEventListener('dragover', e => {
  e.preventDefault();
  dropZone.classList.add('drag-over');
});
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('drag-over');
  handleFile(e.dataTransfer.files[0]);
});
dropZone.addEventListener('click', () => fileInput.click());
dropZone.addEventListener('keydown', e => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); }
});
fileInput.addEventListener('change', () => handleFile(fileInput.files[0]));

btnChange.addEventListener('click', () => {
  previewArea.classList.add('hidden');
  dropZone.classList.remove('hidden');
  btnUpload.classList.add('hidden');
  btnUpload.disabled = true;
  selectedFile = null;
  fileInput.value = '';
  setUploadError('');
});

// ── Upload ───────────────────────────────────────────────
btnUpload.addEventListener('click', async () => {
  if (!selectedFile) return;

  btnUpload.disabled = true;
  setUploadError('');
  showStep(stepProcessing);
  progressLog.innerHTML = '';
  addLog('Uploading your photo…');

  const formData = new FormData();
  formData.append('photo', selectedFile);

  let jobId;
  try {
    const res = await fetch(`${API_BASE}/upload`, {
      method: 'POST',
      body: formData,
    });
    const data = await res.json();

    if (!res.ok) {
      throw new Error(data.error || `Upload failed (HTTP ${res.status})`);
    }
    jobId = data.job_id;
    addLog('Photo uploaded. Kaggle GPU is animating it…', 'ok');
  } catch (err) {
    showStep(stepUpload);
    btnUpload.disabled = false;
    setUploadError('Upload error: ' + err.message);
    return;
  }

  startPolling(jobId);
});

// ── Status polling ───────────────────────────────────────
function startPolling(jobId) {
  pollStartTime = Date.now();
  pollInterval  = POLL_INTERVAL_MIN;
  addLog('Starting animation job (ID: ' + jobId + ')…');
  poll(jobId);
}

function stopPolling() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
}

async function poll(jobId) {
  if (Date.now() - pollStartTime > MAX_POLL_TIME) {
    setProcessingError('Timed out waiting for the animation. Please try again.');
    showStep(stepUpload);
    btnUpload.disabled = false;
    return;
  }

  try {
    const res = await fetch(`${API_BASE}/status?job_id=${encodeURIComponent(jobId)}`);
    const data = await res.json();

    if (!res.ok) throw new Error(data.error || `Status check failed (HTTP ${res.status})`);

    const status = data.status; // 'queued' | 'running' | 'complete' | 'error'

    addLog('Status: ' + status + (data.message ? ' — ' + data.message : ''));

    if (status === 'complete') {
      stopPolling();
      showResult(data.video_url, jobId);
      return;
    }

    if (status === 'error') {
      stopPolling();
      setProcessingError('Animation failed: ' + (data.message || 'unknown error'));
      showStep(stepUpload);
      btnUpload.disabled = false;
      return;
    }

    // still queued or running — keep polling with exponential backoff
    pollInterval = Math.min(pollInterval * 1.5, POLL_INTERVAL_MAX);
    pollTimer = setTimeout(() => poll(jobId), pollInterval);

  } catch (err) {
    addLog('Poll error: ' + err.message, 'err');
    pollInterval = Math.min(pollInterval * 1.5, POLL_INTERVAL_MAX);
    pollTimer = setTimeout(() => poll(jobId), pollInterval);
  }
}

// ── Show result ──────────────────────────────────────────
function showResult(videoUrl, jobId) {
  resultVideo.src = videoUrl;
  btnDownload.href = videoUrl;
  btnDownload.setAttribute('download', `cabdi-animate-${jobId}.mp4`);
  showStep(stepResult);
  addLog('Done!', 'ok');
}

// ── Restart ──────────────────────────────────────────────
btnRestart.addEventListener('click', resetAll);
