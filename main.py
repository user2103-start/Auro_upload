"""
Telegram -> M3U8 downloader -> YouTube uploader bot.

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

Usage in Telegram
-----------------
/start   help
/auth    login to YouTube from your phone browser (no PC needed)
/whoami  show your Telegram user id
paste an .m3u8 URL -> bot asks for a title -> downloads -> sends -> uploads
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

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
REDIRECT_URI = f"{PUBLIC_URL}/oauth2callback" if PUBLIC_URL else ""
TOKEN_FILE = os.path.join(tempfile.gettempdir(), "yt_refresh_token.json")

ASK_TITLE = 1
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# chat_id per pending oauth "state" so we can DM the token back
PENDING_AUTH: dict[str, int] = {}
TG_APP: Application | None = None


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
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError((stderr or b"").decode()[-1500:] or "ffmpeg failed")
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 1024:
        raise RuntimeError("Downloaded file is empty. Is the link still live?")


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
    """Blocking resumable upload. Call from a worker thread."""
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
    while response is None:
        _, response = request.next_chunk()
    return response["id"]


# --------------------------------------------------------------------------- #
# Telegram handlers
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    url: str
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
        "Bhej de ek .m3u8 link, main use download karke tere YouTube channel pe "
        f"upload kar dunga ({PRIVACY_STATUS}).\n\n"
        f"YouTube: {connected}\n\n"
        "/auth  – phone browser se YouTube login (PC ki zarurat nahi)\n"
        "/whoami – tera Telegram user id\n"
        "/cancel – abort\n\n"
        "1. Link paste karo\n2. Title bhejo\n3. Done"
    )


async def whoami(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Your Telegram user id: {update.effective_user.id}")


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
    await update.message.reply_text(
        "Ye link phone ke browser me kholo, apne YouTube waale Google account se "
        "login karo aur Allow dabao. Token main khud pakad lunga 👇\n\n" + url,
        disable_web_page_preview=True,
    )


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

    context.user_data["job"] = Job(url=match.group(0))
    await update.message.reply_text("Link mil gaya. Ab video ka title bhejo:")
    return ASK_TITLE


async def got_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    job: Job = context.user_data["job"]
    job.title = (update.message.text or "").strip() or "Untitled"

    msg = await update.message.reply_text("Downloading… (thoda time lagega)")
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    tmpdir = tempfile.mkdtemp(prefix="m3u8-")
    out = os.path.join(tmpdir, "video.mp4")
    try:
        await download_m3u8(job.url, out)
        size_mb = os.path.getsize(out) / (1024 * 1024)
        await msg.edit_text(f"Download done ({size_mb:.1f} MB).")

        # 1) send the file back into the chat
        if SEND_TO_TELEGRAM:
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
                    await update.message.reply_text(f"Telegram pe bhej nahi paya: {exc}")
            else:
                await update.message.reply_text(
                    f"File {size_mb:.1f} MB hai, Telegram bot upload limit "
                    f"{TG_MAX_MB:.0f} MB hai — sirf YouTube pe jaayega."
                )

        # 2) upload to YouTube
        await msg.edit_text(f"Uploading to YouTube… ({size_mb:.1f} MB)")
        video_id = await asyncio.to_thread(
            upload_to_youtube, out, job.title, f"Source: {job.url}"
        )
        await msg.edit_text(f"Ho gaya bhai ✅\nhttps://youtu.be/{video_id}")
    except Exception as exc:  # noqa: BLE001
        log.exception("job failed")
        await msg.edit_text(f"Fail ho gaya ❌\n\n{exc}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        context.user_data.pop("job", None)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("job", None)
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
                    "Ise Render ke env var *GOOGLE_REFRESH_TOKEN* me paste kar de "
                    "(warna restart pe dobara /auth karna padega):\n\n"
                    f"`{refresh_token}`"
                ),
                parse_mode="Markdown",
            )
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
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(600)
        .build()
    )
    TG_APP = application

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, got_url)],
        states={ASK_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_title)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=600,
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("auth", auth))
    application.add_handler(CommandHandler("whoami", whoami))
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
