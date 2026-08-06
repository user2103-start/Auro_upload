# M3U8 -> YouTube Telegram bot

## Local / Render setup

1. `pip install -r requirements.txt`
2. Get a YouTube refresh token once, on your own machine:
   ```
   export GOOGLE_CLIENT_ID=...
   export GOOGLE_CLIENT_SECRET=...
   python get_refresh_token.py
   ```
3. Deploy on Render as a **Web Service** (free tier):
   - Build command: `pip install -r requirements.txt`
   - Start command: `python main.py`
   - Env vars: `TELEGRAM_BOT_TOKEN`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
     `GOOGLE_REFRESH_TOKEN`, `ALLOWED_USER_IDS`, optional `PRIVACY_STATUS`, `MAX_MINUTES`
   - The bot binds `$PORT` with a `/healthz` endpoint so Render keeps it alive.

## Free tier limits to keep in mind

- 512 MB RAM / 0.1 CPU: we use `ffmpeg -c copy` (remux only, no re-encode).
- Disk is ephemeral and small: each video is written to a temp dir and deleted
  after upload. Keep recordings short (`MAX_MINUTES`).
- Free web services sleep after 15 min idle; the first message after sleep can
  take ~30 s to wake up. A cron ping to `/healthz` avoids that.
- YouTube API quota: 10,000 units/day ≈ 6 uploads/day per project. Ask Google
  for a quota increase if you need more.
- Fresh OAuth apps in "Testing" mode upload as `private` and the refresh token
  expires in 7 days — publish the OAuth consent screen to fix that.

## Future extensions (easy hooks)

- `download_m3u8()` -> swap in `yt-dlp` to support YouTube/other page URLs.
- Direct mp4 links: skip ffmpeg, stream the file straight to `upload_to_youtube()`.
- Telegram video files: `update.message.video.get_file()` then upload.

