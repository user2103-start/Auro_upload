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

4. **Google Cloud Console me redirect URI add karna (bahut zaruri):**
   - APIs & Services → Credentials → OAuth 2.0 Client IDs → apna **Web application** client kholo.
   - **Authorized redirect URIs** → **ADD URI**.
   - Exactly ye daalo (apne Render URL se replace karke):
     ```
     https://<your-service>.onrender.com/oauth2callback
     ```
   - **⚠️ Exact hona chahiye** — `http` nahi, end me `/` nahi, spelling galat nahi.
   - **Save** kar do.
5. Telegram me bot ko `/auth` bhejo. Bot tujhe exact redirect URI aur auth link dega.
   - Link phone ke browser me kholo → apne YouTube waale Google account se login karo → Allow.
   - Bot khud refresh token nikal ke tere chat me bhej dega.
6. Us token ko Render env var `GOOGLE_REFRESH_TOKEN` me paste kar do
   (Render restart pe temp file mit jaati hai, isliye ye permanent fix hai).
7. Ab bhej sakte ho:
   - `.m3u8` link
   - direct video link (`.mp4` / `.mkv` / `.webm`)
   - Telegram pe video ya mp4 file (max ~20 MB — Bot API download limit)

   Flow: source bhejo → title bhejo → bot **pehle YouTube pe upload karta hai**,
   link deta hai, phir chhoti file ho to chat me bhi bhej deta hai. File turant
   server se delete ho jaati hai.

### Extra env vars (optional)

| Name | Default | Kaam |
|---|---|---|
| `MAX_FILE_MB` | `2000` | isse badi file download hi nahi hogi |
| `MIN_FREE_DISK_MB` | `300` | free space itna se kam hua to download abort |
| `PARALLEL_CONNS` | `16` | kitne HLS segments / http chunks ek saath download karein (speed) |

`/disk` command se free space dekh sakte ho aur temp files clean ho jaati hain.


## Common errors

### `Error 400: redirect_uri_mismatch`

Ye tab aata hai jab Google Cloud me daali URI bot ke bheji hui URI se match nahi karti.

Fix:
1. Bot ko `/auth` bhejo.
2. Bot jo exact URI dikhaye (jaise `https://abc.onrender.com/oauth2callback`), usko
   Google Cloud Console → Credentials → Authorized redirect URIs me paste karo.
3. **Save** karo.
4. `/auth` dobara chalao.

Galtiyaan jo log karte hain:
- `http` instead of `https`
- End me `/` lagana: `/oauth2callback/`
- Spelling galat: `/oauth2-callback` ya `/oauthcallback`
- Google Cloud me save kiye bina wapas `/auth` chalana

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
