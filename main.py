#!/usr/bin/env python3
"""Production-ready Telegram Bot: HLS → MP4 → YouTube Upload (Render optimized)"""

import os, json, asyncio, logging, subprocess, tempfile, base64, threading
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
TOKEN_FILE = Path.cwd() / "google_token.json"
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


class YouTubeOAuth:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.credentials = None
        self._load_token_from_file()

    def _load_token_from_file(self):
        """Load token from google_token.json"""
        if TOKEN_FILE.exists():
            try:
                self.credentials = Credentials.from_authorized_user_file(TOKEN_FILE, YOUTUBE_SCOPES)
                logger.info("✅ Loaded token from google_token.json")
                if self.credentials.expired and self.credentials.refresh_token:
                    self._refresh_token()
            except Exception as e:
                logger.warning(f"Failed to load token: {e}")
                self.credentials = None

def _save_token_to_file(self):
    """Save token to google_token.json"""
    if not self.credentials:
        logger.error(f"_save_token_to_file: EARLY RETURN - self.credentials is None")
        return
    
    try:
        with open(TOKEN_FILE, "w") as f:
            f.write(self.credentials.to_json())
        logger.info("✅ Token saved to google_token.json")
    except Exception as e:
        logger.error(f"Failed to save token: {e}")
        logger.error(f"Failed to save token traceback: {traceback.format_exc()}")


@flask_app.route("/oauth/callback", methods=["GET"])
def oauth_callback():
    """Handle OAuth2 callback from Google"""
    global user_id_for_oauth, telegram_app_ref
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")
    if error:
        logger.error(f"OAuth error: {error}")
        return f"❌ Authorization failed: {error}", 400
    if not code or not state:
        return "❌ Missing code or state", 400
    try:
        oauth = YouTubeOAuth(CLIENT_ID, CLIENT_SECRET)
        success = oauth.handle_callback(code, state)
        if success:
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
                    logger.error(f"Failed to send Telegram message: {e}")
            return "✅ Authorization successful! Close this window.", 200
        else:
            return "❌ Failed to obtain token", 400
    except Exception as e:
        logger.error(f"OAuth callback error: {e}")
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


async def check_ffprobe_installed() -> bool:
    try:
        result = await asyncio.to_thread(subprocess.run, ["ffprobe", "-version"], capture_output=True, timeout=5)
        return result.returncode == 0
    except:
        return False


async def download_hls_stream(url: str, output_path: str) -> bool:
    """Download HLS stream using FFmpeg"""
    cmd = ["ffmpeg", "-i", url, "-c", "copy", "-bsf:a", "aac_adtstoasc", "-y", output_path]
    try:
        result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=3600)
        return result.returncode == 0
    except Exception as e:
        logger.error(f"Download error: {e}")
        return False


async def convert_to_mp4(input_path: str, output_path: str) -> bool:
    """Convert to MP4 using FFmpeg"""
    cmd = ["ffmpeg", "-i", input_path, "-c:v", "libx264", "-preset", "medium", "-crf", "23",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "faststart", "-y", output_path]
    try:
        result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=3600)
        return result.returncode == 0
    except Exception as e:
        logger.error(f"Conversion error: {e}")
        return False


async def extract_metadata(video_path: str) -> Dict[str, Any]:
    """Extract metadata using ffprobe"""
    cmd = ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-print_json", video_path]
    try:
        result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return {}
        data = json.loads(result.stdout)
        format_info = data.get("format", {})
        streams = data.get("streams", [])
        video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})
        duration_seconds = float(format_info.get("duration", 0))
        return {
            "duration": str(timedelta(seconds=int(duration_seconds))),
            "size_mb": round(float(format_info.get("size", 0)) / (1024 * 1024), 2),
            "width": video_stream.get("width", "Unknown"),
            "height": video_stream.get("height", "Unknown"),
            "fps": video_stream.get("r_frame_rate", "Unknown"),
            "audio_codec": audio_stream.get("codec_name", "None"),
            "audio_sample_rate": audio_stream.get("sample_rate", "Unknown"),
        }
    except Exception as e:
        logger.error(f"Metadata extraction error: {e}")
        return {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "🎬 *HLS to MP4 Converter & YouTube Uploader*\n\n"
        "Send me an M3U8 URL to start.\n\n"
        "I can:\n"
        "• Download HLS streams\n"
        "• Convert to MP4\n"
        "• Upload to YouTube\n\n"
        "Example: `https://example.com/stream.m3u8`",
        parse_mode="Markdown"
    )
    return WAITING_FOR_URL


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 *Help*\n\n"
        "*Commands:*\n"
        "/start - Start the bot\n"
        "/ytlogin - Authorize YouTube upload\n"
        "/cancel - Cancel current operation\n"
        "/help - Show this message\n\n"
        "*Workflow:*\n"
        "1. Use /ytlogin to authorize YouTube (first time only)\n"
        "2. Send M3U8 URL\n"
        "3. Bot downloads and converts\n"
        "4. Review metadata\n"
        "5. Upload to YouTube (if authorized)",
        parse_mode="Markdown"
    )


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
    global user_id_for_oauth, telegram_app_ref

    try:
        user_id_for_oauth = update.effective_user.id

        oauth = YouTubeOAuth(CLIENT_ID, CLIENT_SECRET)

        if oauth.is_authenticated():
            await update.message.reply_text(
                "✅ Already authenticated with YouTube!"
            )
            return ConversationHandler.END

        auth_url, state = oauth.get_authorization_url()

        message = (
            "<b>🔐 YouTube Authorization</b>\n\n"
            "Click the link below to grant permission:\n\n"
            f'<a href="{auth_url}">Open Authorization Link</a>\n\n'
            "After granting permission, you'll be redirected and I'll send a confirmation."
        )

        await update.message.reply_text(
            message,
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove()
        )

        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Error in ytlogin: {e}")

        await update.message.reply_text(
            f"❌ Error: {str(e)}",
            reply_markup=ReplyKeyboardRemove()
        )

        return ConversationHandler.END


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    url = update.message.text.strip()
    if not re.match(r'https?://', url):
        await update.message.reply_text("❌ Invalid URL. Please provide a valid HTTP/HTTPS URL.", reply_markup=ReplyKeyboardRemove())
        return WAITING_FOR_URL
    context.user_data["url"] = url
    task_temp_dir = TEMP_DIR / f"task_{int(datetime.now().timestamp())}"
    task_temp_dir.mkdir(exist_ok=True)
    context.user_data["temp_dir"] = str(task_temp_dir)
    await update.message.reply_text("⏳ Starting download...", reply_markup=ReplyKeyboardRemove())
    return await handle_download(update, context)


async def handle_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    url = context.user_data["url"]
    temp_dir = Path(context.user_data["temp_dir"])
    if not await check_ffmpeg_installed():
        await update.message.reply_text("❌ FFmpeg not installed. Please install FFmpeg.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    progress_msg = await update.message.reply_text("📥 Downloading...")
    intermediate_file = temp_dir / "intermediate.ts"
    success = await download_hls_stream(url, str(intermediate_file))
    if not success or not intermediate_file.exists():
        await progress_msg.edit_text("❌ Download failed. Check URL and try again.")
        return WAITING_FOR_URL
    context.user_data["intermediate_file"] = str(intermediate_file)
    await progress_msg.edit_text("✅ Download complete. Converting to MP4...")
    return await handle_conversion(update, context)


async def handle_conversion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    temp_dir = Path(context.user_data["temp_dir"])
    intermediate_file = context.user_data["intermediate_file"]
    mp4_file = temp_dir / "output.mp4"
    context.user_data["mp4_file"] = str(mp4_file)
    progress_msg = await update.message.reply_text("⚙️ Converting to MP4...")
    success = await convert_to_mp4(intermediate_file, str(mp4_file))
    if not success or not mp4_file.exists():
        await progress_msg.edit_text("❌ Conversion failed.")
        return WAITING_FOR_URL
    await progress_msg.edit_text("📊 Extracting metadata...")
    metadata = await extract_metadata(str(mp4_file))
    metadata_text = "📹 *Video Metadata:*\n"
    if metadata:
        metadata_text += f"• Duration: {metadata.get('duration', 'N/A')}\n"
        metadata_text += f"• Size: {metadata.get('size_mb', 'N/A')} MB\n"
        metadata_text += f"• Resolution: {metadata.get('width', 'N/A')}x{metadata.get('height', 'N/A')}\n"
        metadata_text += f"• FPS: {metadata.get('fps', 'N/A')}\n"
        metadata_text += f"• Audio: {metadata.get('audio_codec', 'N/A')}\n"
    context.user_data["metadata"] = metadata
    keyboard = [["✅ Upload to YouTube", "❌ Cancel"]]
    await update.message.reply_text(metadata_text + "\nUpload to YouTube?",
                                    reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True),
                                    parse_mode="Markdown")
    return CONFIRMING_UPLOAD


async def handle_upload_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text
    if choice == "❌ Cancel":
        return await cancel(update, context)
    if choice != "✅ Upload to YouTube":
        await update.message.reply_text("Invalid choice. Please try again.")
        return CONFIRMING_UPLOAD
    await update.message.reply_text("📝 Enter video title:", reply_markup=ReplyKeyboardRemove())
    return GETTING_TITLE


async def handle_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["title"] = update.message.text
    await update.message.reply_text("📝 Enter video description (or /skip):")
    return GETTING_DESCRIPTION


async def handle_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    context.user_data["description"] = "" if text == "/skip" else text
    keyboard = [["🌍 Public", "👤 Private", "🔗 Unlisted"]]
    await update.message.reply_text("Select visibility:", reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True))
    return GETTING_VISIBILITY


async def handle_visibility(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text
    visibility_map = {"🌍 Public": "public", "👤 Private": "private", "🔗 Unlisted": "unlisted"}
    if choice not in visibility_map:
        await update.message.reply_text("Invalid choice. Please try again.")
        return GETTING_VISIBILITY
    context.user_data["visibility"] = visibility_map[choice]
    await update.message.reply_text("🚀 Uploading to YouTube...", reply_markup=ReplyKeyboardRemove())
    return await return await handle_youtube_upload(update, context)
    
async def handle_youtube_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Upload to YouTube with resumable upload and progress tracking"""
    max_retries = 3
    retry_count = 0
    progress_msg = None
    
    while retry_count < max_retries:
        try:
            logger.info(f"handle_youtube_upload: Creating YouTubeOAuth instance")
            oauth = YouTubeOAuth(CLIENT_ID, CLIENT_SECRET)
            
            logger.info(f"handle_youtube_upload: Checking is_authenticated()")
            if not oauth.is_authenticated():
                logger.error(f"handle_youtube_upload: NOT AUTHENTICATED - credentials is None")
                logger.error(f"handle_youtube_upload: TOKEN_FILE exists: {TOKEN_FILE.exists()}")
                logger.error(f"handle_youtube_upload: You must run /ytlogin first and see '✅ Token saved to google_token.json' in logs")
                await update.message.reply_text("❌ Not authenticated with YouTube.\n\nUse /ytlogin to authorize first.", reply_markup=ReplyKeyboardRemove())
                return ConversationHandler.END
            
            logger.info(f"handle_youtube_upload: Building YouTube service")
            youtube = oauth.get_youtube_service()
            logger.info(f"handle_youtube_upload: YouTube service ready")
            def upload_with_progress():
                """Perform resumable upload with progress tracking"""
                nonlocal last_update
                response = None
                status = None
                while response is None:
                    status, response = request.next_chunk()
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
                    logger.warning(f"Upload timeout, retrying... ({retry_count}/{max_retries})")
                    await progress_msg.edit_text(f"⏱️ Upload timeout, retrying... ({retry_count}/{max_retries})")
                    await asyncio.sleep(5)
                    continue
                else:
                    await progress_msg.edit_text("❌ Upload failed: Timeout after retries")
                    return ConversationHandler.END
            
            video_id = response.get("id")
            await progress_msg.edit_text("✅ Upload complete! Processing...")
            
            import shutil
            try:
                shutil.rmtree(context.user_data["temp_dir"])
            except:
                pass
            
            context.user_data.clear()
            await update.message.reply_text(
                f"✅ *Upload successful!*\n\n🎬 Video ID: `{video_id}`\n🔗 URL: https://youtube.com/watch?v={video_id}\n\nSend another M3U8 URL to continue.",
                parse_mode="Markdown"
            )
            logger.info(f"✅ Video uploaded successfully: {video_id}")
            return WAITING_FOR_URL
        
        except HttpError as e:
            retry_count += 1
            error_code = e.resp.status
            if 500 <= error_code < 600 and retry_count < max_retries:
                logger.warning(f"Server error {error_code}, retrying...")
                if progress_msg:
                    await progress_msg.edit_text(f"⚠️ Server error ({error_code}), retrying... ({retry_count}/{max_retries})")
                await asyncio.sleep(5)
                continue
            logger.error(f"YouTube API error: {error_code}")
            await update.message.reply_text(f"❌ Upload failed: API error {error_code}", reply_markup=ReplyKeyboardRemove())
            return ConversationHandler.END
        
        except Exception as e:
            logger.error(f"Upload error: {e}\n{traceback.format_exc()}")
            await update.message.reply_text(f"❌ Error: {str(e)}", reply_markup=ReplyKeyboardRemove())
            return ConversationHandler.END
    
    return ConversationHandler.END


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Update {update} caused error {context.error}\n{traceback.format_exc()}")
    try:
        await update.message.reply_text("❌ An error occurred. Please try again.", reply_markup=ReplyKeyboardRemove())
    except:
        pass


def main():
    global telegram_app_ref
    
    print("\n" + "=" * 70)
    print("🚀 HLS → MP4 → YouTube Upload Bot (Production)")
    print("=" * 70)
    
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        if result.returncode != 0:
            print("❌ FFmpeg not installed. Install with: sudo apt install ffmpeg")
            return
    except:
        print("❌ FFmpeg not installed. Install with: sudo apt install ffmpeg")
        return
    
    try:
        result = subprocess.run(["ffprobe", "-version"], capture_output=True, timeout=5)
        if result.returncode != 0:
            print("❌ ffprobe not installed. Install with: sudo apt install ffmpeg")
            return
    except:
        print("❌ ffprobe not installed. Install with: sudo apt install ffmpeg")
        return
    
    print("✅ FFmpeg available")
    print(f"✅ Render External URL: {RENDER_EXTERNAL_URL}")
    print(f"✅ OAuth Redirect URI: {OAUTH_REDIRECT_URI}")
    print(f"✅ Token File: {TOKEN_FILE}")
    
    app = Application.builder().token(BOT_TOKEN).build()
    telegram_app_ref = app
    
    print("\n🌐 Starting Flask OAuth callback server...")
    flask_thread = threading.Thread(target=run_flask_app, daemon=True)
    flask_thread.start()
    print("✅ Flask server started in background")
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_FOR_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url)],
            CONFIRMING_UPLOAD: [MessageHandler(filters.Regex("^(✅ Upload to YouTube|❌ Cancel)$"), handle_upload_confirmation)],
            GETTING_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_title)],
            GETTING_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_description)],
            GETTING_VISIBILITY: [MessageHandler(filters.Regex("^(🌍 Public|👤 Private|🔗 Unlisted)$"), handle_visibility)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )
    
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("ytlogin", ytlogin))
    app.add_handler(conv_handler)
    app.add_error_handler(error_handler)
    
    print("\n" + "=" * 70)
    print("🎯 Bot Started Successfully")
    print("=" * 70)
    print("\n📱 Telegram Commands:")
    print("   /start    - Initialize bot")
    print("   /ytlogin  - Authorize YouTube upload")
    print("   /help     - Show help")
    print("   /cancel   - Cancel operation")
    print("\n" + "=" * 70 + "\n")
    
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

