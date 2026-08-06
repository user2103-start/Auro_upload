#!/usr/bin/env python3
"""Production-ready Telegram Bot: HLS → MP4 → YouTube Upload
Designed for Render + Gunicorn (production WSGI server)"""

import os, json, asyncio, logging, subprocess, tempfile, base64, threading, time, sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple
import re, secrets, traceback

from flask import Flask, request
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters
from telegram.error import TelegramError

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

BOT_TOKEN = os.getenv("BOT_TOKEN")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")

RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
if not RENDER_EXTERNAL_URL:
    render_service_name = os.getenv("RENDER_SERVICE_NAME")
    if render_service_name:
        RENDER_EXTERNAL_URL = f"https://{render_service_name}.onrender.com"
    else:
        RENDER_EXTERNAL_URL = "http://localhost:5000"

PORT = int(os.getenv("PORT", 5000))

if not all([BOT_TOKEN, CLIENT_ID, CLIENT_SECRET]):
    raise RuntimeError("❌ Missing: BOT_TOKEN, CLIENT_ID, CLIENT_SECRET")

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TEMP_DIR = Path(tempfile.gettempdir()) / "telegram_hls_bot"
TEMP_DIR.mkdir(exist_ok=True)

PROCESS_START_TIME = time.time()
PROCESS_PID = os.getpid()

TOKEN_FILE = Path(tempfile.gettempdir()) / "google_token.json"
OAUTH_STATE_DIR = TEMP_DIR / "oauth_states"
OAUTH_STATE_DIR.mkdir(exist_ok=True)

OAUTH_REDIRECT_URI = f"{RENDER_EXTERNAL_URL}/oauth/callback"
OAUTH_STATE_TTL = 600

WAITING_FOR_URL, CONFIRMING_UPLOAD, GETTING_TITLE, GETTING_DESCRIPTION, GETTING_VISIBILITY = range(5)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(sys.stdout)
    ],
    force=True
)
logger = logging.getLogger(__name__)

# ==================== FLASK APP ====================
flask_app = Flask(__name__)
flask_app.secret_key = secrets.token_hex(32)

telegram_app_ref = None
user_id_for_oauth = None

_TOKEN_CACHE = None
_TOKEN_CACHE_TIME = 0


def save_token_to_cache(token_json: str):
    global _TOKEN_CACHE, _TOKEN_CACHE_TIME
    _TOKEN_CACHE = token_json
    _TOKEN_CACHE_TIME = time.time()


def get_token_from_cache() -> Optional[str]:
    global _TOKEN_CACHE, _TOKEN_CACHE_TIME
    if _TOKEN_CACHE and (time.time() - _TOKEN_CACHE_TIME) < 3600:
        return _TOKEN_CACHE
    return None


def log_process_info(context: str):
    elapsed = time.time() - PROCESS_START_TIME
    logger.info(f"[{context}] elapsed={elapsed:.1f}s, PID={os.getpid()}, TOKEN_FILE exists={TOKEN_FILE.exists()}")


def cleanup_expired_oauth_states():
    now = time.time()
    try:
        for state_file in OAUTH_STATE_DIR.glob('*.txt'):
            if (now - state_file.stat().st_mtime) > OAUTH_STATE_TTL:
                state_file.unlink()
    except:
        pass


def validate_oauth_state(state: str) -> bool:
    state_file = OAUTH_STATE_DIR / f"{state}.txt"
    if not state_file.exists():
        return False
    if (time.time() - state_file.stat().st_mtime) > OAUTH_STATE_TTL:
        try:
            state_file.unlink()
        except:
            pass
        return False
    try:
        state_file.unlink()
        return True
    except:
        return False


def create_oauth_flow() -> Flow:
    return Flow.from_client_config({
        "installed": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }, scopes=YOUTUBE_SCOPES, redirect_uri=OAUTH_REDIRECT_URI)


class YouTubeOAuth:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.credentials = None
        self._load_token_from_file()

    def _load_token_from_file(self):
        logger.info(f"[LOAD] Attempting from: {TOKEN_FILE}")
        
        if TOKEN_FILE.exists():
            try:
                with open(TOKEN_FILE, "r") as f:
                    token_json = f.read()
                self.credentials = Credentials.from_authorized_user_info(json.loads(token_json), YOUTUBE_SCOPES)
                logger.info("✅ Loaded from FILE")
                if self.credentials.expired and self.credentials.refresh_token:
                    self._refresh_token()
                return
            except Exception as e:
                logger.error(f"[LOAD] File failed: {e}")
        
        cache_json = get_token_from_cache()
        if cache_json:
            try:
                self.credentials = Credentials.from_authorized_user_info(json.loads(cache_json), YOUTUBE_SCOPES)
                logger.info("✅ Loaded from CACHE")
                if self.credentials.expired and self.credentials.refresh_token:
                    self._refresh_token()
                return
            except Exception as e:
                logger.error(f"[LOAD] Cache failed: {e}")
        
        refresh_token_env = os.getenv("YOUTUBE_REFRESH_TOKEN")
        if refresh_token_env:
            try:
                self.credentials = Credentials(
                    token=None,
                    refresh_token=refresh_token_env,
                    token_uri="https://oauth2.googleapis.com/token",
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                    scopes=YOUTUBE_SCOPES
                )
                logger.info("✅ Loaded from ENV VAR")
                if self.credentials.refresh_token:
                    self._refresh_token()
                return
            except Exception as e:
                logger.error(f"[LOAD] Env failed: {e}")
        
        logger.error("[LOAD] ❌ NO TOKEN FOUND")
        self.credentials = None

    def _save_token_to_file(self):
        if not self.credentials or not self.credentials.token:
            logger.error("[SAVE] No credentials to save")
            return
        
        try:
            token_json = self.credentials.to_json()
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            
            with open(TOKEN_FILE, "w") as f:
                f.write(token_json)
                f.flush()
                os.fsync(f.fileno())  # Force disk sync
            
            logger.info(f"✅ Saved to FILE ({TOKEN_FILE.stat().st_size} bytes)")
            save_token_to_cache(token_json)
            
            if self.credentials.refresh_token:
                os.environ["YOUTUBE_REFRESH_TOKEN"] = self.credentials.refresh_token
                logger.info("✅ Refresh token in env var")
            else:
                logger.warning("⚠️ NO REFRESH_TOKEN (1 hour expiry)")
        except Exception as e:
            logger.error(f"[SAVE] Failed: {e}")

    def _refresh_token(self):
        if not self.credentials or not self.credentials.refresh_token:
            return False
        try:
            self.credentials.refresh(Request())
            logger.info("[REFRESH] ✅ Token refreshed")
            self._save_token_to_file()
            return True
        except Exception as e:
            logger.error(f"[REFRESH] Failed: {e}")
            self.credentials = None
            return False

    def get_authorization_url(self) -> Tuple[str, str]:
        try:
            flow = create_oauth_flow()
            auth_url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true')
            
            state_file = OAUTH_STATE_DIR / f"{state}.txt"
            state_file.touch()
            
            if hasattr(flow, 'code_verifier') and flow.code_verifier:
                verifier_file = OAUTH_STATE_DIR / f"{state}_verifier.txt"
                verifier_file.write_text(flow.code_verifier)
            
            logger.info(f"[OAUTH] ✅ URL generated, state={state[:20]}...")
            return auth_url, state
        except Exception as e:
            logger.error(f"[OAUTH] Failed: {e}")
            raise

    def handle_callback(self, code: str, state: str) -> bool:
        try:
            logger.info(f"[CALLBACK] START: state={state[:20]}...")
            
            if not validate_oauth_state(state):
                logger.error("[CALLBACK] Invalid state")
                return False
            
            flow = create_oauth_flow()
            
            verifier_file = OAUTH_STATE_DIR / f"{state}_verifier.txt"
            if verifier_file.exists():
                try:
                    code_verifier = verifier_file.read_text()
                    if hasattr(flow, 'code_verifier'):
                        flow.code_verifier = code_verifier
                    verifier_file.unlink()
                except:
                    pass
            
            token_response = flow.fetch_token(code=code)
            self.credentials = flow.credentials
            
            if not self.credentials or not self.credentials.token:
                logger.error("[CALLBACK] No credentials")
                return False
            
            logger.info(f"[CALLBACK] ✅ Credentials obtained")
            self._save_token_to_file()
            logger.info("✅ Authorization SUCCESS")
            return True
        except Exception as e:
            logger.error(f"[CALLBACK] Failed: {type(e).__name__}: {e}")
            return False

    def is_authenticated(self) -> bool:
        if not self.credentials:
            logger.warning("[AUTH] No credentials")
            return False
        
        if self.credentials.expired:
            if self.credentials.refresh_token:
                return self._refresh_token()
            else:
                logger.error("[AUTH] ❌ Expired + no refresh_token")
                return False
        
        if not self.credentials.valid:
            if self.credentials.refresh_token:
                return self._refresh_token()
            else:
                logger.error("[AUTH] ❌ Not valid + no refresh_token")
                return False
        
        logger.info("[AUTH] ✅ Valid")
        return True

    def get_youtube_service(self):
        if not self.is_authenticated():
            raise RuntimeError("❌ Not authenticated. Use /ytlogin first.")
        return build("youtube", "v3", credentials=self.credentials)


@flask_app.route("/oauth/callback", methods=["GET"])
def oauth_callback():
    global user_id_for_oauth, telegram_app_ref
    
    logger.info("=" * 80)
    logger.info("[OAUTH_CB] CALLBACK RECEIVED FROM GOOGLE")
    log_process_info("OAUTH_CB")
    
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")
    
    logger.info(f"[OAUTH_CB] code={code[:20] if code else 'None'}...")
    logger.info(f"[OAUTH_CB] state={state[:20] if state else 'None'}...")
    
    if error:
        logger.error(f"[OAUTH_CB] Error: {error}")
        return f"❌ {error}", 400
    
    if not code or not state:
        logger.error("[OAUTH_CB] Missing code or state")
        return "❌ Missing code or state", 400
    
    try:
        oauth = YouTubeOAuth(CLIENT_ID, CLIENT_SECRET)
        success = oauth.handle_callback(code, state)
        
        if not success:
            logger.error("[OAUTH_CB] Failed")
            return "❌ Failed", 400
        
        logger.info(f"[OAUTH_CB] TOKEN_FILE: exists={TOKEN_FILE.exists()}, size={TOKEN_FILE.stat().st_size if TOKEN_FILE.exists() else 0}")
        
        if telegram_app_ref and user_id_for_oauth:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(telegram_app_ref.bot.send_message(
                    chat_id=user_id_for_oauth,
                    text="✅ *YouTube authentication successful!*",
                    parse_mode="Markdown"
                ))
                logger.info("[OAUTH_CB] ✅ Telegram notified")
            except Exception as e:
                logger.error(f"[OAUTH_CB] Telegram failed: {e}")
        
        logger.info("=" * 80)
        return "✅ Authorization successful! Close this window.", 200
    except Exception as e:
        logger.error(f"[OAUTH_CB] Exception: {type(e).__name__}: {e}")
        logger.error(traceback.format_exc())
        return f"❌ Server error", 500


@flask_app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200


# ==================== TELEGRAM HANDLERS ====================

async def download_hls_stream_to_mp4(url: str, output_path: str) -> bool:
    logger.info(f"[DOWNLOAD] Starting")
    cmd = [
        "ffmpeg", "-i", url,
        "-c:v", "copy", "-c:a", "aac", "-bsf:a", "aac_adtstoasc", "-movflags", "faststart",
        "-y", output_path
    ]
    try:
        result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode == 0:
            logger.info(f"[DOWNLOAD] ✅ Success")
            return True
        else:
            logger.error(f"[DOWNLOAD] FFmpeg error")
            return False
    except Exception as e:
        logger.error(f"[DOWNLOAD] Exception: {e}")
        return False


async def extract_metadata_fast(video_path: str) -> Dict[str, Any]:
    try:
        file_size_mb = round(Path(video_path).stat().st_size / (1024 * 1024), 2)
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1:nokey=1", video_path]
        result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=10)
        
        duration_sec = 0
        if result.returncode == 0 and result.stdout.strip():
            try:
                duration_sec = int(float(result.stdout.strip()))
            except:
                pass
        
        return {"duration": str(timedelta(seconds=duration_sec)), "size_mb": file_size_mb}
    except Exception as e:
        logger.error(f"[METADATA] Error: {e}")
        return {"duration": "Unknown", "size_mb": 0}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "🎬 *HLS to MP4 Uploader*\n\nSend me an M3U8 URL.",
        parse_mode="Markdown"
    )
    return WAITING_FOR_URL


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("📖 *Help*\n\n/start, /ytlogin, /cancel", parse_mode="Markdown")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if "temp_dir" in context.user_data:
        import shutil
        try:
            shutil.rmtree(context.user_data["temp_dir"])
        except:
            pass
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def ytlogin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    global user_id_for_oauth
    try:
        logger.info("[YTLOGIN] START")
        user_id_for_oauth = update.effective_user.id
        oauth = YouTubeOAuth(CLIENT_ID, CLIENT_SECRET)
        
        if oauth.is_authenticated():
            await update.message.reply_text("✅ Already authenticated!")
            return WAITING_FOR_URL
        
        auth_url, state = oauth.get_authorization_url()
        await update.message.reply_text(
            f"<b>🔐 YouTube Auth</b>\n\n<a href='{auth_url}'>Click here</a>",
            parse_mode="HTML", reply_markup=ReplyKeyboardRemove()
        )
        return WAITING_FOR_URL
    except Exception as e:
        logger.error(f"[YTLOGIN] Error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    url = update.message.text.strip()
    if not re.match(r'https?://', url):
        await update.message.reply_text("❌ Invalid URL.")
        return WAITING_FOR_URL
    
    context.user_data["url"] = url
    task_temp_dir = TEMP_DIR / f"task_{int(datetime.now().timestamp())}"
    task_temp_dir.mkdir(exist_ok=True)
    context.user_data["temp_dir"] = str(task_temp_dir)
    context.user_data["mp4_file"] = str(task_temp_dir / "output.mp4")
    
    await update.message.reply_text("⏳ Downloading...")
    
    success = await download_hls_stream_to_mp4(url, context.user_data["mp4_file"])
    if not success:
        await update.message.reply_text("❌ Download failed.")
        return WAITING_FOR_URL
    
    metadata = await extract_metadata_fast(context.user_data["mp4_file"])
    context.user_data["metadata"] = metadata
    
    keyboard = [["✅ Upload", "❌ Cancel"]]
    await update.message.reply_text(
        f"📹 *Ready*\n• Size: {metadata.get('size_mb')} MB\n• Duration: {metadata.get('duration')}\n\nUpload?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True), parse_mode="Markdown"
    )
    return CONFIRMING_UPLOAD


async def handle_upload_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text == "❌ Cancel":
        return await cancel(update, context)
    if update.message.text != "✅ Upload":
        return CONFIRMING_UPLOAD
    await update.message.reply_text("📝 Title:", reply_markup=ReplyKeyboardRemove())
    return GETTING_TITLE


async def handle_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["title"] = update.message.text
    await update.message.reply_text("📝 Description (/skip):")
    return GETTING_DESCRIPTION


async def handle_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["description"] = "" if update.message.text == "/skip" else update.message.text
    keyboard = [["🌍 Public", "👤 Private", "🔗 Unlisted"]]
    await update.message.reply_text("Visibility:", reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True))
    return GETTING_VISIBILITY


async def handle_visibility(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    visibility_map = {"🌍 Public": "public", "👤 Private": "private", "🔗 Unlisted": "unlisted"}
    if update.message.text not in visibility_map:
        return GETTING_VISIBILITY
    context.user_data["visibility"] = visibility_map[update.message.text]
    await update.message.reply_text("🚀 Uploading...", reply_markup=ReplyKeyboardRemove())
    return await handle_youtube_upload(update, context)


async def handle_youtube_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    max_retries = 3
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            logger.info(f"[UPLOAD] START (attempt {retry_count + 1}/{max_retries})")
            oauth = YouTubeOAuth(CLIENT_ID, CLIENT_SECRET)
            
            if not oauth.is_authenticated():
                logger.error("[UPLOAD] NOT authenticated")
                await update.message.reply_text("❌ Not authenticated.\n\nUse /ytlogin first.", reply_markup=ReplyKeyboardRemove())
                return ConversationHandler.END
            
            logger.info("[UPLOAD] Building YouTube service...")
            youtube = oauth.get_youtube_service()
            
            video_body = {
                "snippet": {
                    "title": context.user_data["title"],
                    "description": context.user_data["description"],
                },
                "status": {"privacyStatus": context.user_data["visibility"]}
            }
            
            mp4_file = context.user_data["mp4_file"]
            media = MediaFileUpload(mp4_file, mimetype="video/mp4", resumable=True, chunksize=10*1024*1024)
            upload_request = youtube.videos().insert(part="snippet,status", body=video_body, media_body=media)
            
            progress_msg = await update.message.reply_text("📤 0%")
            
            def upload_with_progress():
                response = None
                last_update = 0
                while response is None:
                    status, response = upload_request.next_chunk()
                    if status:
                        progress = int(status.progress() * 100)
                        if progress - last_update >= 10:
                            logger.info(f"Upload: {progress}%")
                            last_update = progress
                return response
            
            try:
                response = await asyncio.wait_for(asyncio.to_thread(upload_with_progress), timeout=3600)
            except asyncio.TimeoutError:
                retry_count += 1
                if retry_count < max_retries:
                    await progress_msg.edit_text(f"⏱️ Timeout, retrying... ({retry_count}/{max_retries})")
                    await asyncio.sleep(5)
                    continue
                await progress_msg.edit_text("❌ Timeout")
                return ConversationHandler.END
            
            video_id = response.get("id")
            
            import shutil
            try:
                shutil.rmtree(context.user_data["temp_dir"])
            except:
                pass
            
            context.user_data.clear()
            await update.message.reply_text(
                f"✅ *Success!*\n\n🎬 ID: `{video_id}`\n🔗 https://youtube.com/watch?v={video_id}",
                parse_mode="Markdown"
            )
            logger.info(f"✅ Upload complete: {video_id}")
            return WAITING_FOR_URL
        
        except HttpError as e:
            retry_count += 1
            error_code = e.resp.status
            logger.error(f"[UPLOAD] HttpError {error_code}")
            if 500 <= error_code < 600 and retry_count < max_retries:
                await update.message.reply_text(f"⚠️ Error {error_code}, retrying...")
                await asyncio.sleep(5)
                continue
            await update.message.reply_text(f"❌ API error {error_code}", reply_markup=ReplyKeyboardRemove())
            return ConversationHandler.END
        
        except Exception as e:
            logger.error(f"[UPLOAD] Exception: {type(e).__name__}: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}", reply_markup=ReplyKeyboardRemove())
            return ConversationHandler.END
    
    return ConversationHandler.END


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Error: {context.error}")


def run_telegram_polling():
    """Run Telegram polling in background thread"""
    global telegram_app_ref
    
    app = Application.builder().token(BOT_TOKEN).build()
    telegram_app_ref = app
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start), CommandHandler("ytlogin", ytlogin)],
        states={
            WAITING_FOR_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url)],
            CONFIRMING_UPLOAD: [MessageHandler(filters.Regex("^(✅ Upload|❌ Cancel)$"), handle_upload_confirmation)],
            GETTING_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_title)],
            GETTING_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_description)],
            GETTING_VISIBILITY: [MessageHandler(filters.Regex("^(🌍 Public|👤 Private|🔗 Unlisted)$"), handle_visibility)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )
    
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(conv_handler)
    app.add_error_handler(error_handler)
    
    logger.info("🤖 Telegram polling started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


def run_cleanup():
    """Cleanup daemon"""
    while True:
        time.sleep(300)
        cleanup_expired_oauth_states()


def main():
    """Entry point for gunicorn"""
    print("\n" + "=" * 80)
    print("[STARTUP] 🚀 HLS → MP4 → YouTube Bot (Production with Gunicorn)")
    print("=" * 80)
    print(f"BOT_TOKEN: {BOT_TOKEN[:20]}..." if BOT_TOKEN else "❌ Missing")
    print(f"RENDER_EXTERNAL_URL: {RENDER_EXTERNAL_URL}")
    print(f"OAUTH_REDIRECT_URI: {OAUTH_REDIRECT_URI}")
    print(f"TOKEN_FILE: {TOKEN_FILE}")
    print(f"PORT: {PORT}")
    print("=" * 80 + "\n")
    
    logger.info("=" * 80)
    logger.info("[STARTUP] HLS → MP4 → YouTube Bot (Gunicorn Production)")
    logger.info(f"RENDER_EXTERNAL_URL: {RENDER_EXTERNAL_URL}")
    logger.info(f"OAUTH_REDIRECT_URI: {OAUTH_REDIRECT_URI}")
    logger.info(f"TOKEN_FILE: {TOKEN_FILE}")
    logger.info(f"PORT: {PORT}")
    logger.info("=" * 80)
    
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5, check=True)
        print("✅ FFmpeg ready")
    except:
        print("❌ FFmpeg not found")
        return
    
    print("🧹 Starting cleanup daemon...")
    threading.Thread(target=run_cleanup, daemon=True).start()
    
    print("🤖 Starting Telegram polling daemon...")
    threading.Thread(target=run_telegram_polling, daemon=True).start()
    
    print("✅ Bot Ready - Waiting for Gunicorn\n")
    
    # Return Flask app for gunicorn to run
    return flask_app


# ==================== ENTRY POINT ====================
if __name__ == "__main__":
    app = main()

    logger.info(f"Starting Flask server on port {PORT}")

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=True
    )
else:
    app = main()
