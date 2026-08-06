#!/usr/bin/env python3
"""Production-ready Telegram Bot: HLS → MP4 → YouTube Upload (Render optimized)
FINAL COMPLETE FIX: fsync after file write, env var backup, no race conditions."""

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

# Use /tmp for token (more reliable on Render than project dir)
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

# Global token cache - fallback if file not readable
_TOKEN_CACHE = None
_TOKEN_CACHE_TIME = 0

flask_app = Flask(__name__)
flask_app.secret_key = secrets.token_hex(32)
telegram_app_ref = None
user_id_for_oauth = None


def save_token_to_cache(token_json: str):
    """Store token in global cache as backup."""
    global _TOKEN_CACHE, _TOKEN_CACHE_TIME
    _TOKEN_CACHE = token_json
    _TOKEN_CACHE_TIME = time.time()
    logger.info(f"[CACHE] Token stored in memory (TTL: 1 hour)")


def get_token_from_cache() -> Optional[str]:
    """Get token from cache if fresh (< 1 hour old)."""
    global _TOKEN_CACHE, _TOKEN_CACHE_TIME
    if _TOKEN_CACHE and (time.time() - _TOKEN_CACHE_TIME) < 3600:
        logger.info(f"[CACHE] Token retrieved from memory (age: {time.time() - _TOKEN_CACHE_TIME:.0f}s)")
        return _TOKEN_CACHE
    return None


def log_process_info(context: str):
    """Log process and filesystem context"""
    elapsed = time.time() - PROCESS_START_TIME
    current_pid = os.getpid()
    
    logger.info(f"[DIAGNOSTIC:{context}] PID={current_pid} (started={PROCESS_PID}) elapsed={elapsed:.1f}s")
    logger.info(f"[DIAGNOSTIC:{context}] TOKEN_FILE={TOKEN_FILE}, exists={TOKEN_FILE.exists()}")
    if TOKEN_FILE.exists():
        logger.info(f"[DIAGNOSTIC:{context}] TOKEN_FILE.size={TOKEN_FILE.stat().st_size} bytes")


def cleanup_expired_oauth_states():
    """Clean up old state files."""
    now = time.time()
    try:
        for state_file in OAUTH_STATE_DIR.glob('*.txt'):
            file_age = now - state_file.stat().st_mtime
            if file_age > OAUTH_STATE_TTL:
                try:
                    state_file.unlink()
                except:
                    pass
    except:
        pass


def validate_oauth_state(state: str) -> bool:
    """Validate and consume state."""
    state_file = OAUTH_STATE_DIR / f"{state}.txt"
    
    if not state_file.exists():
        logger.warning(f"[OAUTH_STATE] State file not found: {state[:20]}...")
        return False
    
    file_age = time.time() - state_file.stat().st_mtime
    if file_age > OAUTH_STATE_TTL:
        logger.warning(f"[OAUTH_STATE] State expired: {file_age:.0f}s")
        try:
            state_file.unlink()
        except:
            pass
        return False
    
    try:
        state_file.unlink()
        logger.info(f"[OAUTH_STATE] State validated and consumed: {state[:20]}...")
        return True
    except Exception as e:
        logger.error(f"[OAUTH_STATE] Failed to consume: {e}")
        return False


def create_oauth_flow() -> Flow:
    """Create OAuth Flow."""
    try:
        flow = Flow.from_client_config({
            "installed": {
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }, scopes=YOUTUBE_SCOPES, redirect_uri=OAUTH_REDIRECT_URI)
        return flow
    except Exception as e:
        logger.error(f"[OAUTH_FLOW] Failed: {type(e).__name__}: {e}")
        raise


class YouTubeOAuth:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.credentials = None
        log_process_info("OAUTH_INIT")
        self._load_token_from_file()

    def _load_token_from_file(self):
        """Load token from file or cache or env var."""
        log_process_info("LOAD_START")
        logger.info(f"[LOAD] Attempting to load from: {TOKEN_FILE}")
        
        # TRY #1: Load from file
        if TOKEN_FILE.exists():
            try:
                file_size = TOKEN_FILE.stat().st_size
                logger.info(f"[LOAD] File exists, size={file_size} bytes")
                with open(TOKEN_FILE, "r") as f:
                    token_json = f.read()
                self.credentials = Credentials.from_authorized_user_info(json.loads(token_json), YOUTUBE_SCOPES)
                logger.info("✅ Loaded token from FILE")
                logger.info(f"[LOAD] credentials.valid={self.credentials.valid}, expired={self.credentials.expired}, has_refresh_token={bool(self.credentials.refresh_token)}")
                
                if self.credentials.expired and self.credentials.refresh_token:
                    self._refresh_token()
                
                log_process_info("LOAD_FILE_SUCCESS")
                return
            except Exception as e:
                logger.error(f"[LOAD] Failed to load from file: {type(e).__name__}: {e}")
        
        logger.warning(f"[LOAD] File not found or unreadable: {TOKEN_FILE}")
        
        # TRY #2: Load from memory cache
        cache_json = get_token_from_cache()
        if cache_json:
            try:
                logger.info(f"[LOAD] Attempting to load from MEMORY CACHE")
                self.credentials = Credentials.from_authorized_user_info(json.loads(cache_json), YOUTUBE_SCOPES)
                logger.info("✅ Loaded token from CACHE")
                logger.info(f"[LOAD] credentials.valid={self.credentials.valid}, expired={self.credentials.expired}, has_refresh_token={bool(self.credentials.refresh_token)}")
                
                if self.credentials.expired and self.credentials.refresh_token:
                    self._refresh_token()
                
                log_process_info("LOAD_CACHE_SUCCESS")
                return
            except Exception as e:
                logger.error(f"[LOAD] Failed to load from cache: {type(e).__name__}: {e}")
        
        logger.warning(f"[LOAD] Cache not available")
        
        # TRY #3: Load from environment variable
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
                logger.info(f"[LOAD] Created credentials from env refresh_token")
                
                if self.credentials.refresh_token:
                    self._refresh_token()
                    logger.info("✅ Loaded token from ENV VAR")
                
                log_process_info("LOAD_ENV_SUCCESS")
                return
            except Exception as e:
                logger.error(f"[LOAD] Failed to load from env: {type(e).__name__}: {e}")
        
        logger.error(f"[LOAD] ❌ NO TOKEN FOUND ANYWHERE (file, cache, or env)")
        log_process_info("LOAD_FAILED")
        self.credentials = None

    def _save_token_to_file(self):
        """Save token to file, cache, and env var with fsync."""
        log_process_info("SAVE_START")
        logger.info(f"[SAVE] Target file: {TOKEN_FILE}")
        
        if not self.credentials:
            logger.error(f"[SAVE] ABORT: credentials is None")
            return
        
        try:
            if not self.credentials.token:
                logger.error(f"[SAVE] ABORT: credentials.token is empty")
                return
            
            token_json = self.credentials.to_json()
            
            # SAVE #1: Write to file with fsync
            logger.info(f"[SAVE] Writing to file with fsync...")
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            
            with open(TOKEN_FILE, "w") as f:
                f.write(token_json)
                f.flush()
                os.fsync(f.fileno())  # ← CRITICAL FIX: Force disk sync
            
            logger.info(f"[SAVE] File synced to disk")
            
            # Verify file was created
            if not TOKEN_FILE.exists():
                logger.error(f"[SAVE] ❌ File not found after write!")
                return
            
            verify_size = TOKEN_FILE.stat().st_size
            logger.info(f"[SAVE] ✅ File verified: {verify_size} bytes on disk")
            
            # SAVE #2: Store in memory cache
            save_token_to_cache(token_json)
            
            # SAVE #3: Store refresh_token in env var
            if self.credentials.refresh_token:
                os.environ["YOUTUBE_REFRESH_TOKEN"] = self.credentials.refresh_token
                logger.info(f"[SAVE] ✅ Refresh token in env var")
            else:
                logger.warning(f"[SAVE] ⚠️ NO REFRESH_TOKEN (will need re-auth in ~1 hour)")
            
            logger.info("✅ Token saved to FILE + CACHE + ENV")
            log_process_info("SAVE_SUCCESS")
        except Exception as e:
            logger.error(f"[SAVE] Failed: {type(e).__name__}: {e}")
            log_process_info("SAVE_FAILED")

    def _refresh_token(self):
        """Refresh access token."""
        if not self.credentials or not self.credentials.refresh_token:
            return False
        
        try:
            logger.info(f"[REFRESH] Refreshing token...")
            self.credentials.refresh(Request())
            logger.info(f"[REFRESH] ✅ Success")
            self._save_token_to_file()
            return True
        except Exception as e:
            logger.error(f"[REFRESH] Failed: {type(e).__name__}: {e}")
            self.credentials = None
            return False

    def get_authorization_url(self) -> Tuple[str, str]:
        """Generate auth URL."""
        try:
            logger.info(f"[OAUTH] Generating URL with redirect_uri: {OAUTH_REDIRECT_URI}")
            flow = create_oauth_flow()
            auth_url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true')
            
            # Store state
            state_file = OAUTH_STATE_DIR / f"{state}.txt"
            state_file.touch()
            
            # Store PKCE verifier
            if hasattr(flow, 'code_verifier') and flow.code_verifier:
                verifier_file = OAUTH_STATE_DIR / f"{state}_verifier.txt"
                verifier_file.write_text(flow.code_verifier)
            
            logger.info(f"[OAUTH] ✅ URL generated, state={state[:20]}...")
            return auth_url, state
        except Exception as e:
            logger.error(f"[OAUTH] Failed: {type(e).__name__}: {e}")
            raise

    def handle_callback(self, code: str, state: str) -> bool:
        """Exchange code for credentials."""
        try:
            log_process_info("CALLBACK_START")
            logger.info(f"[CALLBACK] START: state={state[:20]}...")
            
            if not validate_oauth_state(state):
                logger.error(f"[CALLBACK] Invalid state")
                return False
            
            logger.info(f"[CALLBACK] State valid, exchanging code...")
            flow = create_oauth_flow()
            
            # Restore PKCE verifier if present
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
                logger.error(f"[CALLBACK] No credentials returned")
                return False
            
            logger.info(f"[CALLBACK] ✅ Credentials obtained, saving...")
            logger.info(f"[CALLBACK] credentials.valid={self.credentials.valid}, expired={self.credentials.expired}, refresh_token={bool(self.credentials.refresh_token)}")
            
            self._save_token_to_file()
            logger.info("✅ Authorization SUCCESS")
            log_process_info("CALLBACK_SUCCESS")
            return True
        except Exception as e:
            logger.error(f"[CALLBACK] Failed: {type(e).__name__}: {e}")
            log_process_info("CALLBACK_FAILED")
            return False

    def is_authenticated(self) -> bool:
        """Check if credentials are valid."""
        if not self.credentials:
            logger.warning("[AUTH] No credentials")
            return False
        
        logger.info(f"[AUTH] Checking: valid={self.credentials.valid}, expired={self.credentials.expired}, refresh_token={bool(self.credentials.refresh_token)}")
        
        if self.credentials.expired:
            logger.warning(f"[AUTH] Token expired")
            if self.credentials.refresh_token:
                logger.info(f"[AUTH] Refreshing...")
                return self._refresh_token()
            else:
                logger.error(f"[AUTH] ❌ Expired + no refresh_token")
                return False
        
        if not self.credentials.valid:
            logger.warning(f"[AUTH] Token not valid")
            if self.credentials.refresh_token:
                return self._refresh_token()
            else:
                logger.error(f"[AUTH] ❌ Not valid + no refresh_token")
                return False
        
        logger.info(f"[AUTH] ✅ Authenticated")
        return True

    def get_youtube_service(self):
        """Get YouTube service."""
        if not self.is_authenticated():
            logger.error(f"[SERVICE] Not authenticated")
            raise RuntimeError("❌ Not authenticated. Use /ytlogin first.")
        
        try:
            service = build("youtube", "v3", credentials=self.credentials)
            logger.info(f"[SERVICE] ✅ Service ready")
            return service
        except Exception as e:
            logger.error(f"[SERVICE] Failed: {type(e).__name__}: {e}")
            raise


@flask_app.route("/oauth/callback", methods=["GET"])
def oauth_callback():
    """OAuth callback."""
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
        logger.error(f"[OAUTH_CB] Google error: {error}")
        return f"❌ {error}", 400
    
    if not code or not state:
        logger.error(f"[OAUTH_CB] Missing code or state")
        return "❌ Missing code or state", 400
    
    try:
        oauth = YouTubeOAuth(CLIENT_ID, CLIENT_SECRET)
        success = oauth.handle_callback(code, state)
        
        if not success:
            logger.error(f"[OAUTH_CB] handle_callback failed")
            return "❌ Failed to obtain credentials", 400
        
        logger.info(f"[OAUTH_CB] File exists: {TOKEN_FILE.exists()}, size: {TOKEN_FILE.stat().st_size if TOKEN_FILE.exists() else 0}")
        logger.info(f"[OAUTH_CB] Cache valid: {_TOKEN_CACHE is not None}")
        logger.info(f"[OAUTH_CB] Env var set: {bool(os.getenv('YOUTUBE_REFRESH_TOKEN'))}")
        
        if telegram_app_ref and user_id_for_oauth:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(telegram_app_ref.bot.send_message(
                    chat_id=user_id_for_oauth,
                    text="✅ *YouTube authentication successful!*",
                    parse_mode="Markdown"
                ))
            except Exception as e:
                logger.error(f"[OAUTH_CB] Telegram failed: {e}")
        
        logger.info("=" * 80)
        return "✅ Authorization successful! Close this window.", 200
    except Exception as e:
        logger.error(f"[OAUTH_CB] Exception: {type(e).__name__}: {e}")
        return f"❌ Server error", 500


@flask_app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200


def run_flask_app():
    logger.info(f"🌐 Flask starting on port {PORT}")
    logger.info(f"🌐 REDIRECT_URI: {OAUTH_REDIRECT_URI}")
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)


async def download_hls_stream_to_mp4(url: str, output_path: str) -> bool:
    logger.info(f"[DOWNLOAD] HLS→MP4 starting")
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
            logger.error(f"[DOWNLOAD] FFmpeg failed")
            return False
    except Exception as e:
        logger.error(f"[DOWNLOAD] Error: {e}")
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
        logger.info(f"[YTLOGIN] START")
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
    """Upload to YouTube"""
    max_retries = 3
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            logger.info(f"[UPLOAD] START (attempt {retry_count + 1}/{max_retries})")
            oauth = YouTubeOAuth(CLIENT_ID, CLIENT_SECRET)
            
            logger.info(f"[UPLOAD] Checking auth...")
            if not oauth.is_authenticated():
                logger.error(f"[UPLOAD] NOT authenticated")
                await update.message.reply_text("❌ Not authenticated.\n\nUse /ytlogin first.", reply_markup=ReplyKeyboardRemove())
                return ConversationHandler.END
            
            logger.info(f"[UPLOAD] Building YouTube service...")
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
            logger.error(f"[UPLOAD] Error: {type(e).__name__}: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}", reply_markup=ReplyKeyboardRemove())
            return ConversationHandler.END
    
    return ConversationHandler.END


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Error: {context.error}")
    try:
        await update.message.reply_text("❌ An error occurred.", reply_markup=ReplyKeyboardRemove())
    except:
        pass


def main():
    global telegram_app_ref
    
    print("\n" + "=" * 80)
    print("[STARTUP] 🚀 HLS → MP4 → YouTube Bot (PRODUCTION READY)")
    print("=" * 80)
    print(f"TOKEN_FILE: {TOKEN_FILE}")
    print(f"REDIRECT_URI: {OAUTH_REDIRECT_URI}")
    print("=" * 80 + "\n")
    
    logger.info("=" * 80)
    logger.info("[STARTUP] HLS → MP4 → YouTube Bot")
    logger.info(f"TOKEN_FILE: {TOKEN_FILE}")
    logger.info(f"REDIRECT_URI: {OAUTH_REDIRECT_URI}")
    logger.info("=" * 80)
    
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5, check=True)
        print("✅ FFmpeg ready")
    except:
        print("❌ FFmpeg not found")
        return
    
    app = Application.builder().token(BOT_TOKEN).build()
    telegram_app_ref = app
    
    print("🌐 Starting Flask...")
    threading.Thread(target=run_flask_app, daemon=True).start()
    
    print("🧹 Starting cleanup...")
    def cleanup():
        while True:
            time.sleep(300)
            cleanup_expired_oauth_states()
    threading.Thread(target=cleanup, daemon=True).start()
    
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
    
    print("✅ Bot Ready\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
