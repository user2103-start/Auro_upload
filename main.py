#!/usr/bin/env python3
"""Production-ready Telegram Bot: HLS → MP4 → YouTube Upload (Render optimized)"""

import os, json, asyncio, logging, subprocess, tempfile, base64, threading, time
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
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "http://localhost:5000")
PORT = int(os.getenv("PORT", 5000))

if not all([BOT_TOKEN, CLIENT_ID, CLIENT_SECRET]):
    raise RuntimeError("❌ Missing: BOT_TOKEN, CLIENT_ID, CLIENT_SECRET")

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TEMP_DIR = Path(tempfile.gettempdir()) / "telegram_hls_bot"
TEMP_DIR.mkdir(exist_ok=True)

PROCESS_START_TIME = time.time()
PROCESS_PID = os.getpid()

CURRENT_DIR = Path.cwd()
TOKEN_FILE = CURRENT_DIR / "google_token.json"
OAUTH_REDIRECT_URI = f"{RENDER_EXTERNAL_URL}/oauth/callback"
OAUTH_STATE_STORE = {}

WAITING_FOR_URL, CONFIRMING_UPLOAD, GETTING_TITLE, GETTING_DESCRIPTION, GETTING_VISIBILITY = range(5)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()])
logger = logging.getLogger(__name__)

flask_app = Flask(__name__)
flask_app.secret_key = secrets.token_hex(32)
telegram_app_ref = None
user_id_for_oauth = None


def log_process_info(context: str):
    """Log process and filesystem context"""
    elapsed = time.time() - PROCESS_START_TIME
    current_pid = os.getpid()
    current_cwd = Path.cwd()
    token_abs = TOKEN_FILE.resolve()
    
    logger.info(f"[DIAGNOSTIC:{context}] PID={current_pid} (started={PROCESS_PID}) elapsed={elapsed:.1f}s")
    logger.info(f"[DIAGNOSTIC:{context}] cwd={current_cwd}")
    logger.info(f"[DIAGNOSTIC:{context}] TOKEN_FILE={TOKEN_FILE}")
    logger.info(f"[DIAGNOSTIC:{context}] TOKEN_FILE.resolve()={token_abs}")
    logger.info(f"[DIAGNOSTIC:{context}] exists={TOKEN_FILE.exists()}")
    if TOKEN_FILE.exists():
        logger.info(f"[DIAGNOSTIC:{context}] size={TOKEN_FILE.stat().st_size}")
        logger.info(f"[DIAGNOSTIC:{context}] mtime={datetime.fromtimestamp(TOKEN_FILE.stat().st_mtime)}")


class YouTubeOAuth:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.credentials = None
        log_process_info("OAUTH_INIT")
        self._load_token_from_file()

    def _load_token_from_file(self):
        """Load token from google_token.json or environment variable (refresh_token backup)"""
        log_process_info("LOAD_START")
        
        if TOKEN_FILE.exists():
            try:
                file_size = TOKEN_FILE.stat().st_size
                logger.info(f"[LOAD] File exists, size={file_size} bytes")
                self.credentials = Credentials.from_authorized_user_file(TOKEN_FILE, YOUTUBE_SCOPES)
                logger.info("✅ Loaded token from google_token.json")
                logger.info(f"[LOAD] refresh_token exists: {bool(self.credentials.refresh_token)}")
                
                if self.credentials.expired and self.credentials.refresh_token:
                    logger.info(f"[LOAD] Token expired, attempting refresh")
                    self._refresh_token()
                log_process_info("LOAD_FILE_SUCCESS")
                return
            except Exception as e:
                logger.error(f"[LOAD] Failed to load from file: {type(e).__name__}: {e}")
        
        logger.warning(f"[LOAD] FILE NOT FOUND: {TOKEN_FILE}")
        log_process_info("LOAD_FILE_MISSING")
        
        refresh_token_env = os.getenv("YOUTUBE_REFRESH_TOKEN")
        if refresh_token_env:
            logger.info(f"[LOAD] Attempting to restore from YOUTUBE_REFRESH_TOKEN env var")
            try:
                self.credentials = Credentials(
                    token=None,
                    refresh_token=refresh_token_env,
                    token_uri="https://oauth2.googleapis.com/token",
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                    scopes=YOUTUBE_SCOPES
                )
                logger.info(f"[LOAD] Created credentials from refresh token")
                if self.credentials.refresh_token:
                    self._refresh_token()
                    logger.info("✅ Restored and refreshed token from environment")
                log_process_info("LOAD_ENV_SUCCESS")
                return
            except Exception as e:
                logger.error(f"[LOAD] Failed to restore from env: {type(e).__name__}: {e}")
        
        logger.warning(f"[LOAD] No token found: file={TOKEN_FILE.exists()}, env_var={bool(refresh_token_env)}")
        log_process_info("LOAD_FAILED")
        self.credentials = None

    def _save_token_to_file(self):
        """Save token to google_token.json and environment"""
        log_process_info("SAVE_START")
        
        if not self.credentials:
            logger.error(f"[SAVE] ABORT: credentials is None")
            return
        
        try:
            logger.info(f"[SAVE] Writing to {TOKEN_FILE}")
            token_json = self.credentials.to_json()
            
            with open(TOKEN_FILE, "w") as f:
                bytes_written = f.write(token_json)
            
            logger.info(f"[SAVE] Wrote {bytes_written} bytes")
            
            verify_exists = TOKEN_FILE.exists()
            verify_size = TOKEN_FILE.stat().st_size if verify_exists else 0
            verify_mtime = datetime.fromtimestamp(TOKEN_FILE.stat().st_mtime) if verify_exists else None
            
            logger.info(f"[SAVE] VERIFICATION: exists={verify_exists}, size={verify_size}, mtime={verify_mtime}")
            logger.info("✅ Token saved to google_token.json")
            
            if self.credentials.refresh_token:
                os.environ["YOUTUBE_REFRESH_TOKEN"] = self.credentials.refresh_token
                logger.info(f"[SAVE] Refresh token stored in YOUTUBE_REFRESH_TOKEN env var")
            
            log_process_info("SAVE_SUCCESS")
        except Exception as e:
            logger.error(f"[SAVE] Failed: {type(e).__name__}: {e}")
            logger.error(f"[SAVE] Traceback: {traceback.format_exc()}")
            log_process_info("SAVE_FAILED")

    def _refresh_token(self):
        """Refresh access token using refresh token"""
        if not self.credentials or not self.credentials.refresh_token:
            return False
        
        try:
            logger.info(f"[REFRESH] Refreshing token")
            self.credentials.refresh(Request())
            logger.info(f"[REFRESH] Success")
            self._save_token_to_file()
            logger.info("🔄 Access token refreshed")
            return True
        except Exception as e:
            logger.error(f"[REFRESH] Failed: {type(e).__name__}: {e}")
            self.credentials = None
            return False

    def get_authorization_url(self) -> Tuple[str, str]:
        """Generate authorization URL and state for OAuth callback"""
        try:
            logger.info(f"[OAUTH] Generating authorization URL")
            flow = Flow.from_client_config({
                "installed": {
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }, scopes=YOUTUBE_SCOPES, redirect_uri=OAUTH_REDIRECT_URI)
            
            auth_url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true')
            logger.info(f"[OAUTH] Generated state: {state[:20]}...")
            
            OAUTH_STATE_STORE[state] = flow
            return auth_url, state
        except Exception as e:
            logger.error(f"[OAUTH] Failed: {type(e).__name__}: {e}")
            raise

    def handle_callback(self, code: str, state: str) -> bool:
        """Exchange authorization code for credentials"""
        try:
            log_process_info("CALLBACK_START")
            logger.info(f"[CALLBACK] START")
            
            if state not in OAUTH_STATE_STORE:
                logger.error(f"[CALLBACK] Invalid state")
                return False
            
            flow = OAUTH_STATE_STORE.pop(state)
            logger.info(f"[CALLBACK] Calling flow.fetch_token()")
            token_response = flow.fetch_token(code=code)
            
            self.credentials = flow.credentials
            logger.info(f"[CALLBACK] credentials type: {type(self.credentials).__name__ if self.credentials else 'NoneType'}")
            
            if not self.credentials:
                logger.error(f"[CALLBACK] CRITICAL: credentials is None after fetch_token()")
                return False
            
            logger.info(f"[CALLBACK] Credentials obtained, saving...")
            self._save_token_to_file()
            logger.info("✅ Authorization successful")
            log_process_info("CALLBACK_SUCCESS")
            return True
        except Exception as e:
            logger.error(f"[CALLBACK] Exception: {type(e).__name__}: {e}")
            log_process_info("CALLBACK_FAILED")
            return False

    def is_authenticated(self) -> bool:
        """Check if authenticated and refresh if needed"""
        if not self.credentials:
            return False
        
        if self.credentials.expired and self.credentials.refresh_token:
            logger.info(f"[AUTH] Refreshing expired token")
            return self._refresh_token()
        
        return True

    def get_youtube_service(self):
        """Get authenticated YouTube service"""
        if not self.is_authenticated():
            raise RuntimeError("❌ Not authenticated. Use /ytlogin first.")
        
        try:
            service = build("youtube", "v3", credentials=self.credentials)
            logger.info(f"[SERVICE] YouTube service built")
            return service
        except Exception as e:
            logger.error(f"[SERVICE] Failed: {type(e).__name__}: {e}")
            raise


@flask_app.route("/oauth/callback", methods=["GET"])
def oauth_callback():
    """Handle OAuth2 callback from Google"""
    global user_id_for_oauth, telegram_app_ref
    
    logger.info("=" * 70)
    logger.info("[OAUTH_CB] CALLBACK RECEIVED")
    log_process_info("OAUTH_CB")
    logger.info("=" * 70)
    
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")
    
    if error:
        logger.error(f"[OAUTH_CB] Google error: {error}")
        return f"❌ Authorization failed: {error}", 400
    
    if not code or not state:
        return "❌ Missing code or state", 400
    
    try:
        oauth = YouTubeOAuth(CLIENT_ID, CLIENT_SECRET)
        success = oauth.handle_callback(code, state)
        
        if not success:
            logger.error(f"[OAUTH_CB] handle_callback failed")
            log_process_info("OAUTH_CB_FAILED")
            logger.info("=" * 70)
            return "❌ Failed to obtain credentials", 400
        
        token_exists = TOKEN_FILE.exists()
        token_size = TOKEN_FILE.stat().st_size if token_exists else 0
        logger.info(f"[OAUTH_CB] After success: exists={token_exists}, size={token_size}")
        log_process_info("OAUTH_CB_AFTER_SAVE")
        
        if telegram_app_ref and user_id_for_oauth:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(telegram_app_ref.bot.send_message(
                    chat_id=user_id_for_oauth,
                    text="✅ *YouTube authentication successful!*\n\nYou can now upload videos.",
                    parse_mode="Markdown"
                ))
            except Exception as e:
                logger.error(f"[OAUTH_CB] Failed to send Telegram: {type(e).__name__}: {e}")
        
        logger.info("=" * 70)
        return "✅ Authorization successful! Close this window.", 200
    except Exception as e:
        logger.error(f"[OAUTH_CB] Exception: {type(e).__name__}: {e}")
        log_process_info("OAUTH_CB_EXCEPTION")
        logger.info("=" * 70)
        return f"❌ Server error: {str(e)}", 500


@flask_app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200


def run_flask_app():
    """Run Flask app"""
    logger.info(f"🌐 Starting Flask OAuth server on port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)


async def check_ffmpeg_installed() -> bool:
    try:
        result = await asyncio.to_thread(subprocess.run, ["ffmpeg", "-version"], capture_output=True, timeout=5)
        return result.returncode == 0
    except:
        return False


async def download_hls_stream_to_mp4(url: str, output_path: str) -> bool:
    """Download HLS directly to MP4 without intermediate TS file - OPTIMIZED FOR SPEED"""
    logger.info(f"[DOWNLOAD] Starting direct HLS→MP4")
    cmd = [
        "ffmpeg",
        "-i", url,
        "-c:v", "copy",
        "-c:a", "aac",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "faststart",
        "-y",
        output_path
    ]
    try:
        result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode == 0:
            logger.info(f"[DOWNLOAD] SUCCESS")
            return True
        else:
            logger.error(f"[DOWNLOAD] FFmpeg failed: {result.stderr[:500]}")
            return False
    except Exception as e:
        logger.error(f"[DOWNLOAD] Error: {type(e).__name__}: {e}")
        return False


async def extract_metadata_fast(video_path: str) -> Dict[str, Any]:
    """Fast metadata extraction"""
    logger.info(f"[METADATA] Extracting (fast mode)")
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
        
        return {
            "duration": str(timedelta(seconds=duration_sec)),
            "size_mb": file_size_mb,
        }
    except Exception as e:
        logger.error(f"[METADATA] Error: {type(e).__name__}: {e}")
        return {"duration": "Unknown", "size_mb": 0}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "🎬 *HLS to MP4 Uploader*\n\n"
        "Send me an M3U8 URL to convert and upload.\n\n"
        "Example: `https://example.com/stream.m3u8`",
        parse_mode="Markdown"
    )
    return WAITING_FOR_URL


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 *Help*\n\n"
        "*Commands:*\n"
        "/start - Start\n"
        "/ytlogin - Authorize YouTube\n"
        "/cancel - Cancel\n",
        parse_mode="Markdown"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if "temp_dir" in context.user_data:
        import shutil
        try:
            logger.info(f"[CLEANUP] Removing temp dir: {context.user_data['temp_dir']}")
            shutil.rmtree(context.user_data["temp_dir"])
            logger.info(f"[CLEANUP] Temp dir removed")
        except Exception as e:
            logger.error(f"[CLEANUP] Failed to remove temp dir: {e}")
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def ytlogin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Authorize YouTube access - ENTERS CONVERSATION"""
    global user_id_for_oauth, telegram_app_ref

    try:
        log_process_info("YTLOGIN_START")
        logger.info(f"[YTLOGIN] START")
        user_id_for_oauth = update.effective_user.id

        oauth = YouTubeOAuth(CLIENT_ID, CLIENT_SECRET)

        if oauth.is_authenticated():
            logger.info(f"[YTLOGIN] Already authenticated")
            await update.message.reply_text("✅ Already authenticated with YouTube!")
            return WAITING_FOR_URL

        auth_url, state = oauth.get_authorization_url()

        message = (
            "<b>🔐 YouTube Authorization</b>\n\n"
            "Click the link below:\n\n"
            f'<a href="{auth_url}">Open Authorization Link</a>'
        )

        await update.message.reply_text(message, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
        log_process_info("YTLOGIN_AUTH_URL_SENT")
        return WAITING_FOR_URL

    except Exception as e:
        logger.error(f"[YTLOGIN] Error: {type(e).__name__}: {e}")
        log_process_info("YTLOGIN_FAILED")
        await update.message.reply_text(f"❌ Error: {str(e)}", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    url = update.message.text.strip()
    if not re.match(r'https?://', url):
        await update.message.reply_text("❌ Invalid URL.", reply_markup=ReplyKeyboardRemove())
        return WAITING_FOR_URL
    
    log_process_info("HANDLE_URL_START")
    logger.info(f"[URL] Received: {url[:50]}...")
    context.user_data["url"] = url
    task_temp_dir = TEMP_DIR / f"task_{int(datetime.now().timestamp())}"
    task_temp_dir.mkdir(exist_ok=True)
    context.user_data["temp_dir"] = str(task_temp_dir)
    context.user_data["mp4_file"] = str(task_temp_dir / "output.mp4")
    
    await update.message.reply_text("⏳ Downloading and converting...", reply_markup=ReplyKeyboardRemove())
    
    success = await download_hls_stream_to_mp4(url, context.user_data["mp4_file"])
    if not success:
        logger.error(f"[PROCESS] Download failed")
        await update.message.reply_text("❌ Download/conversion failed.", reply_markup=ReplyKeyboardRemove())
        return WAITING_FOR_URL
    
    logger.info(f"[PROCESS] Conversion complete")
    metadata = await extract_metadata_fast(context.user_data["mp4_file"])
    context.user_data["metadata"] = metadata
    
    metadata_text = f"📹 *Video Ready*\n• Size: {metadata.get('size_mb', '?')} MB\n• Duration: {metadata.get('duration', '?')}\n\nUpload to YouTube?"
    keyboard = [["✅ Upload", "❌ Cancel"]]
    await update.message.reply_text(metadata_text, reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True), parse_mode="Markdown")
    return CONFIRMING_UPLOAD


async def handle_upload_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text
    if choice == "❌ Cancel":
        return await cancel(update, context)
    if choice != "✅ Upload":
        return CONFIRMING_UPLOAD
    await update.message.reply_text("📝 Enter video title:", reply_markup=ReplyKeyboardRemove())
    return GETTING_TITLE


async def handle_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["title"] = update.message.text
    await update.message.reply_text("📝 Description (or /skip):")
    return GETTING_DESCRIPTION


async def handle_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    context.user_data["description"] = "" if text == "/skip" else text
    keyboard = [["🌍 Public", "👤 Private", "🔗 Unlisted"]]
    await update.message.reply_text("Visibility:", reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True))
    return GETTING_VISIBILITY


async def handle_visibility(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text
    visibility_map = {"🌍 Public": "public", "👤 Private": "private", "🔗 Unlisted": "unlisted"}
    if choice not in visibility_map:
        return GETTING_VISIBILITY
    context.user_data["visibility"] = visibility_map[choice]
    await update.message.reply_text("🚀 Uploading...", reply_markup=ReplyKeyboardRemove())
    return await handle_youtube_upload(update, context)


async def handle_youtube_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Upload to YouTube with resumable upload"""
    max_retries = 3
    retry_count = 0
    progress_msg = None
    
    while retry_count < max_retries:
        try:
            log_process_info("UPLOAD_START")
            logger.info(f"[UPLOAD] START")
            oauth = YouTubeOAuth(CLIENT_ID, CLIENT_SECRET)
            
            if not oauth.is_authenticated():
                logger.error(f"[UPLOAD] NOT AUTHENTICATED")
                log_process_info("UPLOAD_NOT_AUTH")
                await update.message.reply_text("❌ Not authenticated.\n\nUse /ytlogin first.", reply_markup=ReplyKeyboardRemove())
                return ConversationHandler.END
            
            youtube = oauth.get_youtube_service()
            
            video_body = {
                "snippet": {
                    "title": context.user_data["title"],
                    "description": context.user_data["description"],
                },
                "status": {"privacyStatus": context.user_data["visibility"]}
            }
            
            mp4_file = context.user_data["mp4_file"]
            media = MediaFileUpload(mp4_file, mimetype="video/mp4", resumable=True, chunksize=10 * 1024 * 1024)
            upload_request = youtube.videos().insert(part="snippet,status", body=video_body, media_body=media)
            
            progress_msg = await update.message.reply_text("📤 Uploading... 0%")
            last_update = 0
            
            def upload_with_progress():
                nonlocal last_update
                response = None
                while response is None:
                    status, response = upload_request.next_chunk()
                    if status:
                        progress = int(status.progress() * 100)
                        if progress - last_update >= 10:
                            last_update = progress
                            logger.info(f"Upload progress: {progress}%")
                return response
            
            try:
                response = await asyncio.wait_for(asyncio.to_thread(upload_with_progress), timeout=3600)
            except asyncio.TimeoutError:
                retry_count += 1
                if retry_count < max_retries:
                    if progress_msg:
                        await progress_msg.edit_text(f"⏱️ Timeout, retrying... ({retry_count}/{max_retries})")
                    await asyncio.sleep(5)
                    continue
                else:
                    await progress_msg.edit_text("❌ Upload timeout")
                    return ConversationHandler.END
            
            video_id = response.get("id")
            if progress_msg:
                await progress_msg.edit_text("✅ Uploading...")
            
            import shutil
            try:
                logger.info(f"[CLEANUP] Removing upload temp dir")
                shutil.rmtree(context.user_data["temp_dir"])
            except Exception as e:
                logger.error(f"[CLEANUP] Failed: {e}")
            
            context.user_data.clear()
            await update.message.reply_text(
                f"✅ *Success!*\n\n🎬 ID: `{video_id}`\n🔗 https://youtube.com/watch?v={video_id}",
                parse_mode="Markdown"
            )
            logger.info(f"✅ Upload complete: {video_id}")
            log_process_info("UPLOAD_SUCCESS")
            return WAITING_FOR_URL
        
        except HttpError as e:
            retry_count += 1
            error_code = e.resp.status
            if 500 <= error_code < 600 and retry_count < max_retries:
                if progress_msg:
                    await progress_msg.edit_text(f"⚠️ Error {error_code}, retrying...")
                await asyncio.sleep(5)
                continue
            await update.message.reply_text(f"❌ Upload failed: API error {error_code}", reply_markup=ReplyKeyboardRemove())
            return ConversationHandler.END
        
        except Exception as e:
            logger.error(f"[UPLOAD] Error: {type(e).__name__}: {e}")
            log_process_info("UPLOAD_EXCEPTION")
            await update.message.reply_text(f"❌ Error: {str(e)}", reply_markup=ReplyKeyboardRemove())
            return ConversationHandler.END
    
    return ConversationHandler.END


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Error: {context.error}\n{traceback.format_exc()}")
    try:
        await update.message.reply_text("❌ An error occurred.", reply_markup=ReplyKeyboardRemove())
    except:
        pass


def main():
    global telegram_app_ref
    
    logger.info("=" * 70)
    logger.info("[STARTUP] HLS → MP4 → YouTube Bot")
    log_process_info("STARTUP")
    logger.info("=" * 70)
    
    print("\n" + "=" * 70)
    print("🚀 HLS → MP4 → YouTube Bot (Render Optimized)")
    print("=" * 70)
    print(f"Process ID: {PROCESS_PID}")
    print(f"Start time: {datetime.fromtimestamp(PROCESS_START_TIME)}")
    
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        if result.returncode != 0:
            print("❌ FFmpeg not installed")
            return
    except:
        print("❌ FFmpeg not installed")
        return
    
    print("✅ FFmpeg ready")
    print(f"✅ Token: {TOKEN_FILE}")
    print(f"✅ URL: {OAUTH_REDIRECT_URI}")
    
    app = Application.builder().token(BOT_TOKEN).build()
    telegram_app_ref = app
    
    print("🌐 Starting Flask server...")
    flask_thread = threading.Thread(target=run_flask_app, daemon=True)
    flask_thread.start()
    
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
    
    print("\n" + "=" * 70)
    print("✅ Bot Ready")
    print("=" * 70 + "\n")
    
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
                            
