"""
Clip Saver backend — unified media extraction for TikTok, X/Twitter, and
public Instagram Reels/posts, using yt-dlp + ffmpeg for extraction and
format normalization (HLS/DASH -> single mp4).

Endpoints:
  GET  /health
  GET  /extract?url=...              -> MediaResult JSON (metadata + format list)
  GET  /download?url=...&format_id=  -> streams the actual media file
  GET  /logs                         -> last N structured diagnostic entries

Run locally:  uvicorn main:app --reload --port 8000
Deploy:       see README.md (Render.com, Docker)
"""

import logging
import os
import shutil
import tempfile
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Optional

import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

app = FastAPI(title="Clip Saver Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # public media only — no user auth/cookies involved
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("clipsaver")

# In-memory ring buffer of structured diagnostic events (no secrets/cookies logged).
LOG_BUFFER = deque(maxlen=200)


def log_event(stage: str, **fields):
    entry = {"ts": round(time.time(), 3), "stage": stage, **fields}
    LOG_BUFFER.append(entry)
    logger.info("%s | %s", stage, fields)


def detect_platform(url: str) -> str:
    u = url.lower()
    if "tiktok.com" in u:
        return "tiktok"
    if "twitter.com" in u or "x.com" in u:
        return "twitter"
    if "instagram.com" in u:
        return "instagram"
    return "unknown"


def pick_formats(info: dict) -> list:
    """Reduce yt-dlp's raw format list to a clean, deduped quality ladder."""
    raw = info.get("formats") or []
    candidates = []
    for f in raw:
        if f.get("vcodec") in (None, "none"):
            continue  # audio-only entries; we want playable video formats
        height = f.get("height") or 0
        candidates.append({
            "format_id": f.get("format_id"),
            "height": height,
            "ext": f.get("ext"),
            "has_audio": f.get("acodec") not in (None, "none"),
            "protocol": f.get("protocol", ""),
            "filesize_approx": f.get("filesize") or f.get("filesize_approx"),
        })
    # Prefer formats that already include audio (avoids a merge step when possible)
    candidates.sort(key=lambda c: (c["has_audio"], c["height"]), reverse=True)
    seen_heights = set()
    result = []
    for c in candidates:
        if c["height"] in seen_heights:
            continue
        seen_heights.add(c["height"])
        result.append(c)
    return result[:5]


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/extract")
def extract(url: str = Query(...)):
    platform = detect_platform(url)
    log_event("request", url=url, platform=platform)

    if platform == "unknown":
        log_event("extraction", status="rejected", reason="unsupported_platform")
        raise HTTPException(400, "Unsupported platform — paste a TikTok, X/Twitter, or Instagram link.")

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        reason = "private_or_login_required" if "login" in msg.lower() or "private" in msg.lower() else "extraction_failed"
        log_event("extraction", status="failed", reason=reason, detail=msg[:300])
        friendly = {
            "private_or_login_required": "This post is private or requires login — can't be accessed without the owner's credentials.",
            "extraction_failed": "Couldn't extract media from that link. It may be deleted, region-locked, or an unsupported post type.",
        }[reason]
        raise HTTPException(422, friendly)

    if info.get("_type") == "playlist" and info.get("entries"):
        info = info["entries"][0]  # carousels: first item for now

    formats = pick_formats(info)
    log_event(
        "extraction", status="ok", platform=platform,
        candidates=len(info.get("formats") or []), usable_formats=len(formats),
        duration=info.get("duration"),
    )

    if not formats:
        log_event("normalization", status="no_video_track")
        raise HTTPException(422, "No downloadable video track found on that post.")

    best = formats[0]
    result = {
        "id": f"{platform[:2]}_{info.get('id')}",
        "platform": platform,
        "type": "video",
        "title": (info.get("title") or info.get("description") or "Untitled").strip(),
        "author": f"@{info.get('uploader_id') or info.get('uploader') or ''}".strip(),
        "cover": info.get("thumbnail"),
        "duration": info.get("duration"),
        "hashtags": [f"#{t}" for t in (info.get("tags") or []) if t],
        "formats": formats,
        "download_url": f"/download?url={_q(url)}&format_id={best['format_id']}",
    }
    return JSONResponse(result)


def _q(url: str) -> str:
    from urllib.parse import quote
    return quote(url, safe="")


@app.get("/download")
def download(url: str = Query(...), format_id: Optional[str] = Query(None)):
    platform = detect_platform(url)
    job_id = uuid.uuid4().hex[:8]
    tmpdir = Path(tempfile.mkdtemp(prefix=f"clip_{job_id}_"))
    outtmpl = str(tmpdir / "%(id)s.%(ext)s")

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": outtmpl,
        "format": (f"{format_id}+bestaudio/best" if format_id else "best"),
        "merge_output_format": "mp4",
    }

    log_event("download", status="started", job=job_id, platform=platform, format_id=format_id)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)
            # merge_output_format may change the extension after postprocessing
            candidates = list(tmpdir.glob("*"))
            if candidates:
                filepath = str(max(candidates, key=lambda p: p.stat().st_size))
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        log_event("download", status="failed", job=job_id, detail=str(e)[:300])
        raise HTTPException(422, "Download failed — the source link may have expired. Try fetching the link again.")

    size = os.path.getsize(filepath)
    if size == 0:
        shutil.rmtree(tmpdir, ignore_errors=True)
        log_event("download", status="failed", job=job_id, detail="zero_byte_file")
        raise HTTPException(422, "Downloaded file was empty — please try again.")

    log_event("download", status="completed", job=job_id, size=size)

    response = FileResponse(
        filepath,
        media_type="video/mp4",
        filename=f"clip_{job_id}.mp4",
    )
    response.background = _cleanup_task(tmpdir)
    return response


def _cleanup_task(tmpdir: Path):
    from starlette.background import BackgroundTask
    return BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True)


@app.get("/logs")
def logs():
    return list(LOG_BUFFER)[-100:]
