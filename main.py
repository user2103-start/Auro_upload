"""
Telegram -> M3U8 downloader -> YouTube uploader bot.

Designed for Render free tier (single web service, 512 MB RAM, ephemeral disk).

Required environment variables
------------------------------
TELEGRAM_BOT_TOKEN     from @BotFather
GOOGLE_CLIENT_ID       Google Cloud OAuth client id
GOOGLE_CLIENT_SECRET   Google Cloud OAuth client secret
GOOGLE_REFRESH_TOKEN   from get_refresh_token.py (run once locally)
ALLOWED_USER_IDS       comma separated Telegram user ids allowed to use the bot
Optional
--------
PORT                   provided by Render automatically
PRIVACY_STATUS         private | unlisted | public   (default: unlisted)
MAX_MINUTES            hard cap on recording length (default: 120)

Usage in Telegram
-----------------
/start                 help
just paste an .m3u8 URL -> bot asks for a title -> downloads -> uploads
/cancel                abort the current conversation
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field

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
GOOGLE_REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]
ALLOWED_USER_IDS = {
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
}
PRIVACY_STATUS = os.environ.get("PRIVACY_STATUS", "unlisted")
MAX_MINUTES = int(os.environ.get("MAX_MINUTES", "120"))
PORT = int(os.environ.get("PORT", "10000"))

ASK_TITLE = 1

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


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
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
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
    await update.message.reply_text(
        "Bhej de ek .m3u8 link, main use download karke tere YouTube channel pe "
        f"upload kar dunga ({PRIVACY_STATUS}).\n\n"
        "1. Link paste karo\n2. Title bhejo\n3. Done\n\n"
        f"Your Telegram user id: {update.effective_user.id}\n"
        "/cancel se abort kar sakte ho."
    )


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
        await msg.edit_text(f"Download done ({size_mb:.1f} MB). Uploading to YouTube…")

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
# Render keep-alive web server (free web services must bind $PORT)
# --------------------------------------------------------------------------- #
async def run_health_server() -> None:
    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("health server listening on :%s", PORT)


async def main() -> None:
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(600)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, got_url)],
        states={ASK_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_title)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=600,
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(conv)

    await run_health_server()

    async with application:
        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
        log.info("bot is running")
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
    
