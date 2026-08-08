"""
Telegram -> (M3U8 / MP4 URL / MP4 file) -> YouTube uploader bot.

Designed for Render free tier (single web service, 512 MB RAM, ephemeral disk).

Required environment variables
------------------------------
TELEGRAM_BOT_TOKEN     from @BotFather
GOOGLE_CLIENT_ID       Google Cloud OAuth client id   (type: Web application)
GOOGLE_CLIENT_SECRET   Google Cloud OAuth client secret
PUBLIC_URL             your Render URL, e.g. https://my-bot.onrender.com
                       (add PUBLIC_URL + /oauth2callback as an
                        "Authorized redirect URI" in Google Cloud Console)

Optional
--------
GOOGLE_REFRESH_TOKEN   if you already have one; otherwise use /auth in the bot
ALLOWED_USER_IDS       comma separated Telegram user ids allowed to use the bot
PORT                   provided by Render automatically
PRIVACY_STATUS         private | unlisted | public   (default: unlisted)
MAX_MINUTES            hard cap on recording length (default: 120)
SEND_TO_TELEGRAM       1 | 0  -> also send the mp4 back into the chat (default 1)
TG_MAX_MB              max size to try sending on Telegram (default 50)
MAX_FILE_MB            hard cap on downloaded file size (default 2000)
MIN_FREE_DISK_MB       abort download if free disk drops below this (default 300)

Usage in Telegram
-----------------
/start   help
/auth    login to YouTube from your phone browser (no PC needed)
/whoami  show your Telegram user id
/disk    show free disk space on the server
paste an .m3u8 or .mp4 URL  -> bot asks for a title -> downloads -> uploads
send a video / mp4 document -> bot asks for a title -> uploads
/cancel  abort the current conversation
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
from dataclasses import dataclass, field

import aiohttp
from aiohttp import web
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("m3u8-bot")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
ALLOWED_USER_IDS = {
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
}
PRIVACY_STATUS = os.environ.get("PRIVACY_STATUS", "unlisted")
MAX_MINUTES = int(os.environ.get("MAX_MINUTES", "120"))
PORT = int(os.environ.get("PORT", "10000"))
SEND_TO_TELEGRAM = os.environ.get("SEND_TO_TELEGRAM", "1") not in ("0", "false", "no")
TG_MAX_MB = float(os.environ.get("TG_MAX_MB", "50"))
MAX_FILE_MB = float(os.environ.get("MAX_FILE_MB", "2000"))
MIN_FREE_DISK_MB = float(os.environ.get("MIN_FREE_DISK_MB", "300"))

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
REDIRECT_URI = f"{PUBLIC_URL}/oauth2callback" if PUBLIC_URL else ""
TOKEN_FILE = os.path.join(tempfile.gettempdir(), "yt_refresh_token.json")
WORK_ROOT = os.path.join(tempfile.gettempdir(), "bot-work")

ASK_TITLE = 1
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# chat_id per pending oauth "state" so we can DM the token back
PENDING_AUTH: dict[str, int] = {}
TG_APP: Application | None = None


# --------------------------------------------------------------------------- #
# disk helpers
# --------------------------------------------------------------------------- #
def free_disk_mb(path: str | None = None) -> float:
    usage = shutil.disk_usage(path or tempfile.gettempdir())
    return usage.free / (1024 * 1024)


def cleanup_workdir() -> None:
    """Kill any leftover temp dirs from crashed/killed jobs."""
    shutil.rmtree(WORK_ROOT, ignore_errors=True)
    os.makedirs(WORK_ROOT, exist_ok=True)


# --------------------------------------------------------------------------- #
# refresh token storage (env var first, then the file written by /auth)
# --------------------------------------------------------------------------- #
def save_refresh_token(token: str) -> None:
    with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
        json.dump({"refresh_token": token}, fh)


def load_refresh_token() -> str | None:
    env = os.environ.get("GOOGLE_REFRESH_TOKEN")
    if env:
        return env
    try:
        with open(TOKEN_FILE, encoding="utf-8") as fh:
            return json.load(fh).get("refresh_token")
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# ffmpeg
# --------------------------------------------------------------------------- #
def ffmpeg_path() -> str:
    """Render's Python runtime has no ffmpeg, so we ship a static binary via pip."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


FFMPEG = ffmpeg_path()


def looks_like_hls(url: str) -> bool:
    clean = url.split("?")[0].lower()
    return clean.endswith(".m3u8") or ".m3u8" in url.lower()


async def _watch_disk(out_path: str, proc: asyncio.subprocess.Process) -> str | None:
    """Kill the download if it grows past the cap or the disk is about to fill."""
    reason: str | None = None
    while proc.returncode is None:
        await asyncio.sleep(3)
        try:
            size_mb = os.path.getsize(out_path) / (1024 * 1024)
        except OSError:
            size_mb = 0.0
        if size_mb > MAX_FILE_MB:
            reason = f"File {size_mb:.0f} MB se bada ho gaya (limit {MAX_FILE_MB:.0f} MB)."
        elif free_disk_mb() < MIN_FREE_DISK_MB:
            reason = "Server ka disk bharne waala tha, download rok diya."
        if reason:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return reason
    return None


async def download_m3u8(url: str, out_path: str) -> None:
    """Remux the HLS stream straight into an mp4 (no re-encode -> low CPU/RAM)."""
    cmd = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-user_agent",
        "Mozilla/5.0",
        "-headers",
        "Referer: {}\r\n".format(url.split("/")[0] + "//" + url.split("/")[2]),
        "-i",
        url,
        "-t",
        str(MAX_MINUTES * 60),
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        "-movflags",
        "+faststart",
        out_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    watcher = asyncio.create_task(_watch_disk(out_path, proc))
    _, stderr = await proc.communicate()
    abort_reason = await watcher
    if abort_reason:
        raise RuntimeError(abort_reason)
    if proc.returncode != 0:
        raise RuntimeError((stderr or b"").decode()[-1500:] or "ffmpeg failed")
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 1024:
        raise RuntimeError("Downloaded file is empty. Is the link still live?")


async def download_direct(url: str, out_path: str) -> None:
    """Stream a direct video URL (mp4/mkv/webm...) to disk in small chunks."""
    timeout = aiohttp.ClientTimeout(total=None, sock_read=120)
    written = 0
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"Link ne HTTP {resp.status} diya.")
            declared = resp.headers.get("Content-Length")
            if declared and int(declared) / (1024 * 1024) > MAX_FILE_MB:
                raise RuntimeError(
                    f"File {int(declared) / (1024 * 1024):.0f} MB hai, "
                    f"limit {MAX_FILE_MB:.0f} MB."
                )
            with open(out_path, "wb") as fh:
                async for chunk in resp.content.iter_chunked(1024 * 512):
                    fh.write(chunk)
                    written += len(chunk)
                    if written / (1024 * 1024) > MAX_FILE_MB:
                        raise RuntimeError(f"File limit {MAX_FILE_MB:.0f} MB se bada hai.")
                    if written % (1024 * 1024 * 32) < 1024 * 512 and free_disk_mb() < MIN_FREE_DISK_MB:
                        raise RuntimeError("Server ka disk bharne waala tha, download rok diya.")
    if written < 1024:
        raise RuntimeError("Downloaded file is empty. Link check karo.")


async def download_any(url: str, out_path: str) -> None:
    if looks_like_hls(url):
        await download_m3u8(url, out_path)
    else:
        await download_direct(url, out_path)


# --------------------------------------------------------------------------- #
# YouTube
# --------------------------------------------------------------------------- #
def youtube_client():
    refresh_token = load_refresh_token()
    if not refresh_token:
        raise RuntimeError("YouTube connected nahi hai. Pehle /auth chalao.")
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    creds.refresh(GoogleRequest())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload_to_youtube(path: str, title: str, description: str) -> str:
    """Blocking resumable upload with retries. Call from a worker thread."""
    yt = youtube_client()
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "categoryId": "22",
        },
        "status": {"privacyStatus": PRIVACY_STATUS, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(path, chunksize=4 * 1024 * 1024, resumable=True, mimetype="video/mp4")
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    errors = 0
    while response is None:
        try:
            _, response = request.next_chunk()
        except Exception as exc:  # noqa: BLE001
            errors += 1
            if errors > 8:
                raise
            log.warning("upload chunk failed (%s/8): %s", errors, exc)
            import time

            time.sleep(min(2**errors, 60))
    return response["id"]


# --------------------------------------------------------------------------- #
# Telegram handlers
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    url: str = ""
    tg_file_id: str = ""
    source_label: str = ""
    title: str = ""
    extras: dict = field(default_factory=dict)


def authorized(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


async def start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    connected = "✅ connected" if load_refresh_token() else "❌ not connected (/auth chalao)"
    await update.message.reply_text(
        "Main ye sab le sakta hoon:\n"
        "• .m3u8 link\n"
        "• direct video link (.mp4 / .mkv / .webm)\n"
        "• Telegram pe bheji hui video ya mp4 file (max ~20 MB, Bot API limit)\n\n"
        f"Sab kuch tere YouTube channel pe jaayega ({PRIVACY_STATUS}). "
        f"Chhoti file ({TG_MAX_MB:.0f} MB tak) chat me bhi bhej dunga, badi ka "
        "sirf YouTube link aayega.\n\n"
        f"YouTube: {connected}\n\n"
        "/auth  – phone browser se YouTube login\n"
        "/whoami – tera Telegram user id\n"
        "/disk – server ka free space\n"
        "/cancel – abort"
    )


async def whoami(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Your Telegram user id: {update.effective_user.id}")


async def disk(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    cleanup_workdir()
    await update.message.reply_text(
        f"Free disk: {free_disk_mb():.0f} MB (temp files clean kar diye)."
    )


async def auth(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await update.message.reply_text("Sorry, you are not allowed to use this bot.")
        return
    if not REDIRECT_URI:
        await update.message.reply_text(
            "PUBLIC_URL env var set nahi hai. Render pe apna service URL daalo "
            "(e.g. https://my-bot.onrender.com) aur Google Cloud me "
            "<PUBLIC_URL>/oauth2callback ko Authorized redirect URI me add karo."
        )
        return

    state = secrets.token_urlsafe(16)
    PENDING_AUTH[state] = update.effective_chat.id
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    from urllib.parse import urlencode

    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    # NOTE: no parse_mode here — Markdown eats the underscores in client_id/redirect_uri
    await update.message.reply_text(
        "📋 Pehle ye exact URL Google Cloud Console me add karo:\n\n"
        f"{REDIRECT_URI}\n\n"
        "Path: APIs & Services → Credentials → OAuth 2.0 Client IDs → "
        "Authorized redirect URIs → ADD URI → paste → Save.\n\n"
        "Uske baad neeche wali link phone ke browser me kholo, apne YouTube waale "
        "Google account se login karo aur Allow dabao. Token main khud pakad lunga 👇",
        disable_web_page_preview=True,
    )
    await update.message.reply_text(url, disable_web_page_preview=True)


async def exchange_code(code: str) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        ) as resp:
            data = await resp.json()
    if "refresh_token" not in data:
        raise RuntimeError(json.dumps(data)[:500])
    return data["refresh_token"]


async def got_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not authorized(update):
        await update.message.reply_text("Sorry, you are not allowed to use this bot.")
        return ConversationHandler.END

    match = URL_RE.search(update.message.text or "")
    if not match:
        await update.message.reply_text("Valid http(s) link nahi mila. Dobara try karo.")
        return ConversationHandler.END

    url = match.group(0)
    kind = "HLS (.m3u8)" if looks_like_hls(url) else "direct video link"
    context.user_data["job"] = Job(url=url, source_label=url)
    await update.message.reply_text(f"{kind} mil gaya. Ab video ka title bhejo:")
    return ASK_TITLE


async def got_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Telegram video / mp4 document -> upload to YouTube."""
    if not authorized(update):
        await update.message.reply_text("Sorry, you are not allowed to use this bot.")
        return ConversationHandler.END

    msg = update.message
    media = msg.video or msg.document
    if media is None:
        return ConversationHandler.END

    name = getattr(media, "file_name", None) or "video.mp4"
    mime = (getattr(media, "mime_type", "") or "").lower()
    if msg.document and not (mime.startswith("video/") or name.lower().endswith(
        (".mp4", ".mkv", ".webm", ".mov", ".m4v")
    )):
        await msg.reply_text("Ye video file nahi lagti. mp4/mkv/webm bhejo.")
        return ConversationHandler.END

    size_mb = (media.file_size or 0) / (1024 * 1024)
    if size_mb > 20:
        await msg.reply_text(
            f"File {size_mb:.1f} MB hai. Bot API se main sirf 20 MB tak ki file "
            "download kar sakta hoon 😕 Uska direct link (mp4 URL) bhej de, "
            "phir main YouTube pe daal dunga."
        )
        return ConversationHandler.END

    context.user_data["job"] = Job(tg_file_id=media.file_id, source_label=f"Telegram file: {name}")
    await msg.reply_text("File mil gayi. Ab video ka title bhejo:")
    return ASK_TITLE


async def got_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    job: Job = context.user_data["job"]
    job.title = (update.message.text or "").strip() or "Untitled"

    if free_disk_mb() < MIN_FREE_DISK_MB:
        cleanup_workdir()

    msg = await update.message.reply_text("Downloading… (thoda time lagega)")
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    os.makedirs(WORK_ROOT, exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="job-", dir=WORK_ROOT)
    out = os.path.join(tmpdir, "video.mp4")
    try:
        if job.tg_file_id:
            tg_file = await context.bot.get_file(job.tg_file_id)
            await tg_file.download_to_drive(out)
        else:
            await download_any(job.url, out)

        size_mb = os.path.getsize(out) / (1024 * 1024)
        await msg.edit_text(f"Download done ({size_mb:.1f} MB). YouTube pe bhej raha hoon…")

        # 1) YouTube first — ye zaroori step hai, disk jaldi khaali ho jaaye
        video_id = await asyncio.to_thread(
            upload_to_youtube, out, job.title, f"Source: {job.source_label or job.url}"
        )
        yt_link = f"https://youtu.be/{video_id}"
        await msg.edit_text(f"YouTube pe chala gaya ✅\n{yt_link}")

        # 2) chhoti file ho to chat me bhi bhej do
        if SEND_TO_TELEGRAM and not job.tg_file_id:
            if size_mb <= TG_MAX_MB:
                try:
                    await context.bot.send_chat_action(
                        update.effective_chat.id, ChatAction.UPLOAD_VIDEO
                    )
                    with open(out, "rb") as fh:
                        await context.bot.send_video(
                            chat_id=update.effective_chat.id,
                            video=fh,
                            caption=job.title[:1000],
                            supports_streaming=True,
                            read_timeout=600,
                            write_timeout=600,
                        )
                except Exception as exc:  # noqa: BLE001
                    log.warning("telegram send failed: %s", exc)
                    await update.message.reply_text(
                        f"Chat me bhej nahi paya ({exc}) — YouTube link upar hai 👆"
                    )
            else:
                await update.message.reply_text(
                    f"File {size_mb:.1f} MB hai (Telegram bot limit {TG_MAX_MB:.0f} MB), "
                    f"isliye sirf YouTube pe gayi:\n{yt_link}"
                )
    except Exception as exc:  # noqa: BLE001
        log.exception("job failed")
        await msg.edit_text(f"Fail ho gaya ❌\n\n{exc}")
    finally:
        # file kabhi server pe nahi rukni chahiye
        shutil.rmtree(tmpdir, ignore_errors=True)
        cleanup_workdir()
        context.user_data.pop("job", None)
        log.info("cleanup done, free disk %.0f MB", free_disk_mb())
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("job", None)
    cleanup_workdir()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# Web server: Render keep-alive + Google OAuth callback
# --------------------------------------------------------------------------- #
async def run_web_server() -> None:
    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def oauth2callback(request: web.Request) -> web.Response:
        code = request.query.get("code")
        state = request.query.get("state", "")
        if not code:
            return web.Response(text="Missing code", status=400)
        try:
            refresh_token = await exchange_code(code)
        except Exception as exc:  # noqa: BLE001
            log.exception("token exchange failed")
            return web.Response(text=f"Token exchange failed: {exc}", status=400)

        save_refresh_token(refresh_token)
        chat_id = PENDING_AUTH.pop(state, None)
        if chat_id and TG_APP:
            await TG_APP.bot.send_message(
                chat_id=chat_id,
                text=(
                    "YouTube connect ho gaya ✅\n\n"
                    "Ise Render ke env var GOOGLE_REFRESH_TOKEN me paste kar de "
                    "(warna restart pe dobara /auth karna padega):"
                ),
            )
            # plain text, no Markdown — token me _ aur - hote hain
            await TG_APP.bot.send_message(chat_id=chat_id, text=refresh_token)
        return web.Response(
            text="Done! Telegram pe wapas jao, token bot ne bhej diya hai.",
            content_type="text/plain",
        )

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)
    app.router.add_get("/oauth2callback", oauth2callback)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("web server listening on :%s", PORT)


async def main() -> None:
    global TG_APP
    cleanup_workdir()
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(600)
        .build()
    )
    TG_APP = application

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & ~filters.COMMAND, got_url),
            MessageHandler(filters.VIDEO | filters.Document.ALL, got_file),
        ],
        states={ASK_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_title)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=600,
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("auth", auth))
    application.add_handler(CommandHandler("whoami", whoami))
    application.add_handler(CommandHandler("disk", disk))
    application.add_handler(conv)

    await run_web_server()

    async with application:
        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
        log.info("bot is running")
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
