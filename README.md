# M3U8 -> Telegram + YouTube bot

Sab kuch bot ke andar se hota hai — PC ki zarurat nahi. `get_refresh_token.py`
sirf optional hai (agar laptop ho to).

## Deploy steps (Render free tier)

1. Is repo ko GitHub pe push karo.
2. Render pe **New > Web Service** -> repo select karo.
   - Root directory: `bot`
   - Build command: `pip install -r requirements.txt`
   - Start command: `python main.py`
3. Env vars daalo:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather se |
| `GOOGLE_CLIENT_ID` | Google Cloud (Web application client) |
| `GOOGLE_CLIENT_SECRET` | Google Cloud |
| `PUBLIC_URL` | `https://<your-service>.onrender.com` |
| `ALLOWED_USER_IDS` | apna Telegram id (bot me `/whoami`) |
| `PRIVACY_STATUS` | `unlisted` (optional) |
| `SEND_TO_TELEGRAM` | `1` (optional) |
| `TG_MAX_MB` | `50` (optional) |

4. Google Cloud Console -> APIs & Services -> Credentials -> apna OAuth client
   (Web application) kholo -> **Authorized redirect URIs** me add karo:
   `https://<your-service>.onrender.com/oauth2callback` -> Save.
5. Telegram me bot ko `/auth` bhejo -> link kholo -> Google login -> Allow.
   Bot khud refresh token nikal ke tumhe chat me bhej dega.
6. Us token ko Render env var `GOOGLE_REFRESH_TOKEN` me paste kar do
   (Render restart pe temp file mit jaati hai, isliye ye permanent fix hai).
7. Ab `.m3u8` link paste karo -> title bhejo -> bot video chat me bhejega
   (agar size limit ke andar hai) aur YouTube pe upload karega.

## Telegram file size ki sachai

Telegram *users* 2 GB (premium 4 GB) tak bhej sakte hain, lekin **bots ka
upload limit 50 MB hai** normal Bot API se. 2 GB tak bhejne ke liye apna
"local Bot API server" chalana padta hai — Render free tier pe wo practical
nahi hai. Isliye: chhoti file -> Telegram + YouTube, badi file -> sirf YouTube
(link chat me aa jaayega).

## Free tier limits

- 512 MB RAM / 0.1 CPU: `ffmpeg -c copy` (remux only, no re-encode).
- Disk ephemeral: video temp dir me banta hai, upload ke baad delete.
- Free service 15 min idle ke baad sleep; pehla message ~30 s le sakta hai.
  `/healthz` pe cron ping laga do.
- YouTube quota: 10,000 units/day ≈ 6 uploads/day.
- OAuth consent screen "Testing" me ho to refresh token 7 din me expire hota
  hai — usko **Publish** kar do. Test user me apna mail add rakhna zaruri hai.

## Future hooks

- `download_m3u8()` -> `yt-dlp` daal do, baaki URLs bhi chalenge.
- Direct mp4 link -> ffmpeg skip, seedha `upload_to_youtube()`.
- Telegram video file -> `update.message.video.get_file()` phir upload.
- 
