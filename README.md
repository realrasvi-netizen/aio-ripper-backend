# Clip Saver Backend

Unified extraction for TikTok, X/Twitter, and public Instagram — built on
yt-dlp (does the actual platform-specific extraction and format handling)
and ffmpeg (merges/remuxes streamed formats like HLS into a single mp4).

## Deploy to Render.com (free tier)

1. Push this `backend/` folder to a GitHub repo (or a repo containing just
   these four files: `main.py`, `requirements.txt`, `Dockerfile`, this README).
2. Go to dashboard.render.com → **New → Web Service**.
3. Connect the repo. Render will detect the `Dockerfile` automatically.
4. Instance type: **Free**. Click **Create Web Service**.
5. Wait for the build (installs ffmpeg + yt-dlp, a few minutes the first time).
6. Copy the URL Render gives you, e.g. `https://clip-saver-backend.onrender.com`.
7. Paste that into Clip Saver's "Backend URL" setting.

Free tier note: Render's free web services sleep after 15 minutes of no
traffic and take ~30-60 seconds to wake up on the next request. That's the
tradeoff for $0/month — upgrade to a paid instance if that delay bothers you.

## Endpoints

- `GET /health` — liveness check
- `GET /extract?url=<link>` — returns a MediaResult: title, author, thumbnail,
  duration, hashtags, available formats, and a `download_url`
- `GET /download?url=<link>&format_id=<id>` — streams the actual mp4 file
- `GET /logs` — last 100 structured diagnostic events (no cookies/tokens logged)

## Honest limitations

- **Instagram**: yt-dlp's Instagram support covers a meaningful range of
  public Reels/posts, but Instagram tightens anti-scraping measures often,
  so expect some posts to fail with a clear "extraction failed" error rather
  than working universally. There's no login/cookie support wired in — only
  genuinely public content is attempted, by design.
- **Carousels/slideshows**: this backend currently returns the first item
  of a multi-image/video post. TikTok photo-post slideshow handling still
  goes through the app's existing client-side path, not this backend.
- **Quality selection**: `/extract` returns the available format ladder, but
  the app currently just uses the top one automatically. A quality picker
  in the UI is a small follow-up, not yet wired up.
- **Storage**: files are downloaded to a temp directory per request and
  deleted immediately after streaming — nothing persists between requests.

## Local testing

```
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
curl "http://localhost:8000/extract?url=https://www.tiktok.com/@user/video/123"
```
