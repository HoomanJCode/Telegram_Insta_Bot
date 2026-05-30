import os
import json
import asyncio
import logging
import re
import time
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode
import yt_dlp
from aiohttp import web

import config

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --------------------- DATA HELPERS ---------------------
def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

# Load/save user cookies
user_cookies = load_json(config.USER_COOKIES_FILE, {})  # user_id -> cookie file path
user_videos = load_json(config.USER_VIDEOS_FILE, {})    # user_id -> list of video entries

def save_user_cookies():
    save_json(config.USER_COOKIES_FILE, user_cookies)

def save_user_videos():
    save_json(config.USER_VIDEOS_FILE, user_videos)

# --------------------- URL DETECTION ---------------------
INSTAGRAM_REGEX = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:p|reel|tv|stories|[\w.]+)/([^/?#&]+)"
)

def extract_instagram_info(url: str):
    """Return (media_type, identifier) or None."""
    match = INSTAGRAM_REGEX.search(url)
    if not match:
        return None
    path = match.group(0)
    if "/p/" in path or "/reel/" in path or "/tv/" in path:
        code = match.group(1)
        if "/p/" in path:
            return ("post", code)
        elif "/reel/" in path:
            return ("reel", code)
        elif "/tv/" in path:
            return ("tv", code)
    elif "/stories/" in path:
        username = match.group(1)
        return ("story", username)
    else:
        # profile link
        username = match.group(1)
        return ("profile", username)
    return None

# --------------------- COOKIE HANDLING ---------------------
def get_cookie_path(user_id: int) -> str | None:
    uid = str(user_id)
    if uid in user_cookies and os.path.exists(user_cookies[uid]):
        return user_cookies[uid]
    return None

async def upload_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for /cookies command to upload a cookies.txt file."""
    user_id = update.effective_user.id
    await update.message.reply_text("Send me your Instagram cookies.txt file (exported with 'Get cookies.txt LOCALLY').")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save the uploaded cookies.txt file."""
    user_id = update.effective_user.id
    doc = update.message.document
    if not doc.file_name.endswith(".txt"):
        await update.message.reply_text("Please upload a .txt file (cookies.txt).")
        return
    file = await doc.get_file()
    user_dir = os.path.join(config.COOKIE_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    file_path = os.path.join(user_dir, "cookies.txt")
    await file.download_to_drive(file_path)
    user_cookies[str(user_id)] = file_path
    save_user_cookies()
    await update.message.reply_text("✅ Cookies saved! Now you can send Instagram links.")

# --------------------- DOWNLOAD LOGIC (blocking) ---------------------
def download_instagram_media(url: str, cookie_path: str, media_type: str) -> list[dict]:
    """
    Use yt-dlp to download media.
    Returns a list of dicts: { 'file_path': ..., 'media_type': ..., 'ext': ..., 'title': ... }
    """
    outtmpl = os.path.join(config.DOWNLOAD_DIR, "%(id)s", "%(title)s.%(ext)s")
    ydl_opts = {
        "outtmpl": outtmpl,
        "cookiefile": cookie_path,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "extract_flat": False,
        "format": "best",
        # Instagram often needs to simulate a browser
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    if media_type == "story":
        # stories endpoint handled by yt-dlp natively
        pass
    elif media_type == "profile":
        # only download profile picture (playlist-items 0)
        ydl_opts["playlist_items"] = "0"

    downloaded = []
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            return []
        # For posts with multiple images (carousel), yt-dlp may download each as separate file.
        # We'll scan the output directory for newly created files.
        # Simpler: check the info dict for requested_downloads.
        entries = info.get("entries") or [info]
        for entry in entries:
            if entry is None:
                continue
            # yt-dlp 2023+ uses 'requested_downloads' to list downloaded files
            for rd in entry.get("requested_downloads", []):
                filepath = rd.get("filepath")
                if filepath and os.path.exists(filepath):
                    ext = os.path.splitext(filepath)[1].lower()
                    if ext in (".jpg", ".jpeg"):
                        media = "image"
                    elif ext in (".mp4", ".webm"):
                        media = "video"
                    else:
                        media = "unknown"
                    downloaded.append({
                        "file_path": filepath,
                        "media_type": media,
                        "ext": ext,
                        "title": entry.get("title", "instagram_media"),
                    })
            # Fallback: if requested_downloads empty, look for files by pattern
            if not entry.get("requested_downloads"):
                # Not robust; rely on requested_downloads
                pass
    return downloaded

# --------------------- FORMAT SELECTION & DELIVERY ---------------------
async def process_instagram_link(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    """Fetches info and shows format buttons."""
    user_id = update.effective_user.id
    cookie_path = get_cookie_path(user_id)
    if not cookie_path:
        await update.message.reply_text("❌ Please upload your Instagram cookies using /cookies.")
        return

    info = extract_instagram_info(url)
    if not info:
        await update.message.reply_text("Unsupported Instagram link.")
        return

    media_type, identifier = info
    # Store in context for later use
    context.user_data["insta_url"] = url
    context.user_data["insta_media_type"] = media_type
    context.user_data["insta_identifier"] = identifier

    # Build format options
    keyboard = []
    if media_type == "post":
        keyboard.append([InlineKeyboardButton("🖼 Post Images", callback_data="fmt:post_image")])
        keyboard.append([InlineKeyboardButton("🎥 Post Video", callback_data="fmt:post_video")])
    elif media_type == "reel":
        keyboard.append([InlineKeyboardButton("🎬 Reel Video", callback_data="fmt:reel")])
    elif media_type == "tv":
        keyboard.append([InlineKeyboardButton("📺 IGTV Video", callback_data="fmt:tv")])
    elif media_type == "story":
        keyboard.append([InlineKeyboardButton("📸 Story Image", callback_data="fmt:story_image")])
        keyboard.append([InlineKeyboardButton("🎥 Story Video", callback_data="fmt:story_video")])
    elif media_type == "profile":
        keyboard.append([InlineKeyboardButton("🖼 Profile Picture", callback_data="fmt:profile_pic")])

    keyboard.append([InlineKeyboardButton("🔙 Cancel", callback_data="cancel")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"Detected: {media_type.capitalize()}\nChoose format to download:",
        reply_markup=reply_markup,
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button presses."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "cancel":
        await query.edit_message_text("Download cancelled.")
        return

    if data.startswith("fmt:"):
        format_key = data[4:]
        url = context.user_data.get("insta_url")
        if not url:
            await query.edit_message_text("Session expired. Send the link again.")
            return
        # Start download
        await query.edit_message_text("⏳ Downloading...")
        cookie_path = get_cookie_path(user_id)
        if not cookie_path:
            await query.edit_message_text("❌ Cookies missing. Upload with /cookies.")
            return

        # Map format to media_type for yt-dlp
        yt_media_type = "post" if format_key in ("post_image", "post_video") else (
            "story" if format_key in ("story_image", "story_video") else format_key
        )
        loop = asyncio.get_running_loop()
        try:
            with ThreadPoolExecutor() as pool:
                downloaded_files = await loop.run_in_executor(
                    pool, download_instagram_media, url, cookie_path, yt_media_type
                )
        except Exception as e:
            logger.error(f"Download error: {e}")
            await query.edit_message_text(f"❌ Download failed: {e}")
            return

        if not downloaded_files:
            await query.edit_message_text("❌ No media found. The content may be private or unavailable.")
            return

        # Filter by desired media type
        if format_key == "post_image":
            files = [f for f in downloaded_files if f["media_type"] == "image"]
        elif format_key == "post_video":
            files = [f for f in downloaded_files if f["media_type"] == "video"]
        elif format_key == "story_image":
            files = [f for f in downloaded_files if f["media_type"] == "image"]
        elif format_key == "story_video":
            files = [f for f in downloaded_files if f["media_type"] == "video"]
        else:
            files = downloaded_files

        if not files:
            await query.edit_message_text("No media of the selected type found.")
            return

        # Check for duplicates (by media ID) - simplistic: use file path hash
        # Better: store post code + format in user_videos
        identifier = context.user_data.get("insta_identifier", url)
        entry_key = f"{identifier}_{format_key}"
        uid = str(user_id)
        if uid not in user_videos:
            user_videos[uid] = []
        # Duplicate check
        for existing in user_videos[uid]:
            if existing.get("key") == entry_key:
                await query.edit_message_text("You've already downloaded this exact media.")
                return

        # Prepare delivery options
        total_size = sum(os.path.getsize(f["file_path"]) for f in files)
        # Add to history
        new_entry = {
            "key": entry_key,
            "url": url,
            "format": format_key,
            "files": [{"path": f["file_path"], "media_type": f["media_type"]} for f in files],
            "timestamp": datetime.now().isoformat(),
        }
        user_videos[uid].append(new_entry)
        save_user_videos()

        # Show delivery options
        keyboard = [
            [InlineKeyboardButton("📤 Send on Telegram", callback_data=f"deliver:tg:{entry_key}")],
        ]
        if total_size <= config.MAX_TELEGRAM_FILE_SIZE:
            keyboard[0][0].text += " (inline)"
        keyboard.append([InlineKeyboardButton("🔗 Get Download Link", callback_data=f"deliver:link:{entry_key}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_formats")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            f"Downloaded {len(files)} file(s).\nChoose delivery method:",
            reply_markup=reply_markup,
        )

    elif data.startswith("deliver:"):
        _, method, key = data.split(":", 2)
        uid = str(user_id)
        entries = user_videos.get(uid, [])
        entry = next((e for e in entries if e["key"] == key), None)
        if not entry:
            await query.edit_message_text("Media not found. It may have been cleaned up.")
            return

        if method == "tg":
            await query.edit_message_text("Uploading to Telegram...")
            for file_info in entry["files"]:
                filepath = file_info["path"]
                if not os.path.exists(filepath):
                    await query.message.reply_text(f"File missing: {filepath}")
                    continue
                try:
                    with open(filepath, "rb") as f:
                        if file_info["media_type"] == "image":
                            await query.message.reply_photo(f)
                        else:
                            await query.message.reply_video(f)
                except Exception as e:
                    await query.message.reply_text(f"Failed to send {filepath}: {e}")
            await query.edit_message_text("✅ All files sent!")

        elif method == "link":
            base_link = config.BASE_DOWNLOAD_LINK.rstrip("/")
            links = []
            for file_info in entry["files"]:
                # Generate a unique public path (store under downloads/)
                # We'll serve files directly by their path relative to DOWNLOAD_DIR
                rel_path = os.path.relpath(file_info["path"], config.DOWNLOAD_DIR)
                public_url = f"{base_link}/dl/{rel_path}"
                links.append(f"🔗 {public_url}")
            await query.edit_message_text(
                "Download links (valid while bot runs):\n" + "\n".join(links),
                disable_web_page_preview=True,
            )

    elif data == "back_to_formats":
        # Resend format selection
        await query.edit_message_text("Choose format:")
        # Recreate buttons using stored data
        # (simplified: just ask to send link again)
        await query.message.reply_text("Please send the Instagram link again.")
    else:
        await query.edit_message_text("Unknown action.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check for Instagram links in messages."""
    text = update.message.text
    if not text:
        return
    if INSTAGRAM_REGEX.search(text):
        await process_instagram_link(update, context, text)
    else:
        # Not an Instagram link; maybe they are sending something else.
        pass

# --------------------- RECENT DOWNLOADS WITH PAGINATION ---------------------
async def recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent downloads menu."""
    user_id = update.effective_user.id
    uid = str(user_id)
    entries = user_videos.get(uid, [])
    if not entries:
        await update.message.reply_text("No recent downloads.")
        return

    # Paginate: 5 per page
    page = 0
    if context.args:
        try:
            page = int(context.args[0]) - 1
        except:
            pass
    per_page = 5
    total_pages = (len(entries) + per_page - 1) // per_page
    start = page * per_page
    end = start + per_page
    page_entries = entries[start:end]

    text = f"📥 *Recent Downloads (Page {page+1}/{total_pages})*\n\n"
    for e in page_entries:
        timestamp = datetime.fromisoformat(e["timestamp"]).strftime("%Y-%m-%d %H:%M")
        text += f"• `{e['key']}` - {e['format']}\n  _{timestamp}_\n"

    keyboard = []
    if page > 0:
        keyboard.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"recent_page:{page-1}"))
    if page < total_pages - 1:
        keyboard.append(InlineKeyboardButton("➡️ Next", callback_data=f"recent_page:{page+1}"))
    reply_markup = InlineKeyboardMarkup([keyboard]) if keyboard else None
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)

async def recent_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("recent_page:"):
        page = int(data.split(":")[1])
        uid = str(query.from_user.id)
        entries = user_videos.get(uid, [])
        per_page = 5
        total_pages = (len(entries) + per_page - 1) // per_page
        start = page * per_page
        end = start + per_page
        page_entries = entries[start:end]
        text = f"📥 *Recent Downloads (Page {page+1}/{total_pages})*\n\n"
        for e in page_entries:
            timestamp = datetime.fromisoformat(e["timestamp"]).strftime("%Y-%m-%d %H:%M")
            text += f"• `{e['key']}` - {e['format']}\n  _{timestamp}_\n"
        keyboard = []
        if page > 0:
            keyboard.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"recent_page:{page-1}"))
        if page < total_pages - 1:
            keyboard.append(InlineKeyboardButton("➡️ Next", callback_data=f"recent_page:{page+1}"))
        reply_markup = InlineKeyboardMarkup([keyboard]) if keyboard else None
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)

# --------------------- AUTO-CLEANUP ---------------------
async def cleanup_old_files():
    """Delete files older than STORAGE_DAYS and remove from history."""
    cutoff = datetime.now() - timedelta(days=config.STORAGE_DAYS)
    for uid, entries in user_videos.items():
        new_entries = []
        for e in entries:
            dt = datetime.fromisoformat(e["timestamp"])
            if dt < cutoff:
                # Delete physical files
                for file_info in e["files"]:
                    path = file_info["path"]
                    if os.path.exists(path):
                        os.remove(path)
                        logger.info(f"Removed old file: {path}")
                # Remove empty directories
                dir_path = os.path.dirname(path) if e["files"] else None
                if dir_path and os.path.isdir(dir_path) and not os.listdir(dir_path):
                    shutil.rmtree(dir_path, ignore_errors=True)
            else:
                new_entries.append(e)
        user_videos[uid] = new_entries
    save_user_videos()

async def periodic_cleanup():
    while True:
        await asyncio.sleep(3600)  # every hour
        await cleanup_old_files()

# --------------------- AIOHTTP FILE SERVER ---------------------
async def handle_download_link(request):
    """Serve files from the /dl/ path."""
    file_rel = request.match_info.get("file_path", "")
    # Prevent directory traversal
    file_path = os.path.normpath(os.path.join(config.DOWNLOAD_DIR, file_rel))
    if not file_path.startswith(os.path.abspath(config.DOWNLOAD_DIR)):
        return web.Response(status=403, text="Forbidden")
    if not os.path.exists(file_path):
        return web.Response(status=404, text="File not found")
    return web.FileResponse(file_path)

def run_file_server():
    app = web.Application()
    app.router.add_get("/dl/{file_path:.*}", handle_download_link)
    web.run_app(app, host="0.0.0.0", port=8000)

# --------------------- MAIN BOT ---------------------
async def main():
    # Create necessary directories
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(config.COOKIE_DIR, exist_ok=True)
    os.makedirs("data", exist_ok=True)

    application = Application.builder().token(config.BOT_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("Send an Instagram link or /cookies to upload cookies.")))
    application.add_handler(CommandHandler("cookies", upload_cookies))
    application.add_handler(CommandHandler("recent", recent_command))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_handler, pattern="^(fmt:|deliver:|back_to_formats|recent_page:)"))
    application.add_handler(CallbackQueryHandler(recent_page_callback, pattern="^recent_page:"))

    # Start periodic cleanup
    asyncio.create_task(periodic_cleanup())

    # Run aiohttp server in a separate thread (or we can use asyncio.gather)
    from threading import Thread
    server_thread = Thread(target=run_file_server, daemon=True)
    server_thread.start()

    logger.info("Bot started. Polling...")
    await application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    asyncio.run(main())