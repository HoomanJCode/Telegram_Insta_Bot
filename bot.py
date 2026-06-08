#!/usr/bin/env python3
"""
Instagram Downloader Telegram Bot
Uses gallery-dl for reliable Instagram downloads
Auto-downloads and sends media, caches Telegram file IDs to prevent re-uploads
Cookies stored in RAM only - cleared on bot restart
"""

import os
import sys
import logging
import json
import time
import shutil
import re
import threading
import subprocess
import asyncio
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes
)
from telegram.constants import ParseMode

from config import Config

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.WARNING)
for lib in ('httpx', 'httpcore', 'telegram', 'telegram.ext', 'aiohttp'):
    logging.getLogger(lib).setLevel(logging.WARNING)

logger = logging.getLogger('ig_bot')
logger.setLevel(logging.INFO)
h = logging.StreamHandler()
h.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(h)
logger.propagate = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DATA_DIR = Path('data')
DOWNLOADS_DIR = Path('downloads')
CACHE_FILE = DATA_DIR / 'file_id_cache.json'
WAITING_FOR_COOKIES = 1

INSTAGRAM_RE = re.compile(
    r'(https?://)?(www\.)?instagram\.com/('
    r'p/[^/?#\s]+|'
    r'reel/[^/?#\s]+|'
    r'stories/[^/?#\s]+|'
    r'[^/?#\s]+/?$'
    r')'
)

MAX_IMAGES_PER_MEDIA_GROUP = 10
MAX_CAPTION_LENGTH = 1024

# ---------------------------------------------------------------------------
# File ID Cache (Telegram file IDs only, persisted to disk)
# ---------------------------------------------------------------------------
class FileIDCache:
    """Cache Telegram file IDs per URL to prevent re-uploading"""
    
    def __init__(self, storage_days: int):
        self.storage_days = storage_days
        self._cache: Dict[str, dict] = {}
        self._load()
    
    def _load(self):
        try:
            if CACHE_FILE.exists():
                self._cache = json.loads(CACHE_FILE.read_text())
                logger.info(f"Loaded {len(self._cache)} cached file IDs")
        except Exception as e:
            logger.error(f"Cache load error: {e}")
            self._cache = {}
    
    def _save(self):
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(self._cache, indent=2))
        except Exception as e:
            logger.error(f"Cache save error: {e}")
    
    def get(self, url: str) -> Optional[dict]:
        entry = self._cache.get(url)
        if not entry:
            return None
        
        cached_time = entry.get('cached_time', 0)
        if cached_time:
            age_days = (time.time() - cached_time) / 86400
            if age_days > self.storage_days:
                return None
        
        return entry
    
    def add(self, url: str, file_ids: List[str], title: str = '', username: str = ''):
        self._cache[url] = {
            'file_ids': file_ids,
            'title': title,
            'username': username,
            'cached_time': time.time(),
        }
        self._save()
        logger.info(f"Cached {len(file_ids)} file IDs for {url[:80]}")
    
    def cleanup_expired(self):
        cutoff = time.time() - (self.storage_days * 86400)
        expired = []
        for url, entry in self._cache.items():
            if entry.get('cached_time', 0) < cutoff:
                expired.append(url)
        
        for url in expired:
            self._cache.pop(url, None)
        
        if expired:
            logger.info(f"Cleaned {len(expired)} expired cache entries")
            self._save()

# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
class InstagramDownloaderBot:
    def __init__(self):
        self.config = Config()
        
        for d in (DATA_DIR, DOWNLOADS_DIR):
            d.mkdir(parents=True, exist_ok=True)
        
        # Cookies stored in RAM only - dict of user_id -> temp file path
        self.cookies: Dict[int, str] = {}
        
        self.file_id_cache = FileIDCache(self.config.STORAGE_DAYS)
        self._download_locks: Dict[str, asyncio.Lock] = {}
        
        self._check_gallery_dl()
        self._start_cleanup()
    
    def _check_gallery_dl(self):
        try:
            result = subprocess.run(['gallery-dl', '--version'], capture_output=True, text=True)
            logger.info(f"gallery-dl version: {result.stdout.strip()}")
        except FileNotFoundError:
            logger.error("gallery-dl not found! Installing...")
            subprocess.run([sys.executable, '-m', 'pip', 'install', 'gallery-dl'], check=True)
    
    def _start_cleanup(self):
        def w():
            while True:
                try:
                    self._cleanup()
                except Exception as e:
                    logger.error(f"Cleanup: {e}")
                time.sleep(3600)
        threading.Thread(target=w, daemon=True).start()
    
    def _cleanup(self):
        cutoff = datetime.now() - timedelta(days=self.config.STORAGE_DAYS)
        
        for f in DOWNLOADS_DIR.iterdir():
            if f.is_file() and datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                f.unlink()
                logger.info(f"Cleaned up file: {f.name}")
        
        for d in DOWNLOADS_DIR.iterdir():
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        
        self.file_id_cache.cleanup_expired()
    
    def _ok(self, uid):
        return not self.config.get_whitelist() or uid in self.config.get_whitelist()
    
    def _extract_url(self, text):
        m = INSTAGRAM_RE.search(text)
        if m:
            u = m.group(0)
            if u.startswith('www.'):
                u = 'https://' + u
            elif not u.startswith('http'):
                u = 'https://' + u
            return u.rstrip('/')
        return None
    
    def _get_download_lock(self, url: str) -> asyncio.Lock:
        if url not in self._download_locks:
            self._download_locks[url] = asyncio.Lock()
        return self._download_locks[url]
    
    def _get_unique_download_dir(self, uid: int) -> Path:
        timestamp = int(time.time())
        dir_name = f"{uid}_{timestamp}"
        return DOWNLOADS_DIR / dir_name
    
    def _menu(self, uid):
        has_cookies = uid in self.cookies
        cache_count = len(self.file_id_cache._cache)
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🍪 Upload Cookies", callback_data='c')],
            [InlineKeyboardButton(f"🍪 {'✅' if has_cookies else '❌'} Cookies", callback_data='cookie_status'),
             InlineKeyboardButton(f"💾 {cache_count} cached", callback_data='cache_info')],
        ])
    
    # --- gallery-dl helpers ---
    def _sync_download(self, uid, url):
        """Download Instagram media using gallery-dl"""
        cookie_path = self.cookies[uid]
        output_dir = self._get_unique_download_dir(uid)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        try:
            cmd = ['gallery-dl', '--cookies', cookie_path, '--dest', str(output_dir), url]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            
            if result.returncode != 0:
                raise Exception(f"Download failed: {result.stderr[:200]}")
            
            all_files = sorted(
                [str(f) for f in output_dir.rglob('*') if f.is_file()],
                key=lambda x: Path(x).name
            )
            
            if not all_files:
                raise Exception("No files downloaded")
            
            info = self._sync_get_info(uid, url)
            title = info.get('title', 'Instagram Media')
            username = info.get('username', '')
            
            logger.info(f"Downloaded {len(all_files)} files")
            return all_files, title, username
            
        except Exception as e:
            shutil.rmtree(output_dir, ignore_errors=True)
            raise
    
    def _sync_get_info(self, uid, url):
        """Get media info from gallery-dl"""
        cookie_path = self.cookies[uid]
        
        try:
            cmd = ['gallery-dl', '--cookies', cookie_path, '--dump-json', url]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if isinstance(data, list) and len(data) > 0:
                    first = data[0]
                    if isinstance(first, list) and len(first) >= 2:
                        meta = first[1]
                        if isinstance(meta, dict):
                            return {
                                'title': (meta.get('description', '') or 
                                         f"Post by {meta.get('username', 'Unknown')}").strip(),
                                'username': meta.get('username', ''),
                            }
        except:
            pass
        
        return {'title': 'Instagram Media', 'username': ''}
    
    # --- Telegram upload helpers ---
    def _split_caption(self, text: str, max_len: int = MAX_CAPTION_LENGTH) -> str:
        if len(text) <= max_len:
            return text
        return text[:max_len - 3] + "..."
    
    async def _send_media_batch(self, msg, file_paths: List[str], caption: str):
        """Send media files to Telegram, return file IDs"""
        if not file_paths:
            return []
        
        all_file_ids = []
        
        images = []
        videos = []
        others = []
        
        for fp in file_paths:
            ext = Path(fp).suffix.lower()
            if ext in ('.jpg', '.jpeg', '.png', '.webp'):
                images.append(fp)
            elif ext in ('.mp4', '.webm', '.mkv'):
                videos.append(fp)
            else:
                others.append(fp)
        
        total_images = len(images)
        if total_images > 0:
            batches = [images[i:i + MAX_IMAGES_PER_MEDIA_GROUP] 
                      for i in range(0, total_images, MAX_IMAGES_PER_MEDIA_GROUP)]
            
            for batch_idx, batch in enumerate(batches):
                media_group = []
                batch_caption = ""
                
                if batch_idx == 0 and caption:
                    batch_caption = self._split_caption(caption)
                
                for i, fp in enumerate(batch):
                    with open(fp, 'rb') as f:
                        if i == 0 and batch_caption:
                            media_group.append(InputMediaPhoto(media=f, caption=batch_caption))
                        else:
                            media_group.append(InputMediaPhoto(media=f))
                
                if media_group:
                    try:
                        sent = await msg.reply_media_group(
                            media=media_group,
                            write_timeout=60,
                            read_timeout=60,
                        )
                        for s in sent:
                            if s.photo:
                                all_file_ids.append(s.photo[-1].file_id)
                        logger.info(f"Sent image batch {batch_idx + 1}/{len(batches)}")
                    except Exception as e:
                        logger.error(f"Failed to send image batch: {e}")
                        for fp in batch:
                            try:
                                with open(fp, 'rb') as f:
                                    s = await msg.reply_photo(photo=f)
                                    all_file_ids.append(s.photo[-1].file_id)
                            except Exception as e2:
                                logger.error(f"Failed to send individual image: {e2}")
                
                if len(batches) > 1:
                    await asyncio.sleep(1)
        
        for fp in videos:
            try:
                with open(fp, 'rb') as f:
                    s = await msg.reply_video(
                        video=f,
                        caption=self._split_caption(caption) if not images else None,
                        supports_streaming=True,
                        write_timeout=60,
                    )
                    all_file_ids.append(s.video.file_id)
            except Exception as e:
                logger.error(f"Failed to send video: {e}")
        
        for fp in others:
            try:
                with open(fp, 'rb') as f:
                    s = await msg.reply_document(document=f)
                    all_file_ids.append(s.document.file_id)
            except Exception as e:
                logger.error(f"Failed to send document: {e}")
        
        return all_file_ids
    
    async def _resend_by_file_ids(self, msg, cached_entry: dict):
        """Resend media using cached Telegram file IDs"""
        file_ids = cached_entry.get('file_ids', [])
        title = cached_entry.get('title', '')
        
        if not file_ids:
            return False
        
        try:
            for i, file_id in enumerate(file_ids):
                caption = self._split_caption(title) if i == 0 and title else None
                try:
                    await msg.reply_video(video=file_id, caption=caption, supports_streaming=True)
                except:
                    try:
                        await msg.reply_photo(photo=file_id, caption=caption)
                    except:
                        try:
                            await msg.reply_document(document=file_id, caption=caption)
                        except:
                            return False
                await asyncio.sleep(0.3)
            return True
        except Exception as e:
            logger.error(f"Resend error: {e}")
            return False
    
    # --- Async handlers ---
    async def start_cmd(self, u, c):
        if not self._ok(u.effective_user.id):
            return
        await u.message.reply_text(
            f"👋 Welcome {u.effective_user.first_name}!\n\n"
            "📱 *Instagram Downloader Bot*\n\n"
            "💡 Just send an Instagram link!\n"
            "• Posts → All images/videos\n"
            "• Reels → Video\n"
            "• Stories → Images/videos\n"
            "• Profiles → Profile picture\n\n"
            "🍪 Upload cookies with /cookies\n"
            "⚠️ Cookies stored in RAM only\n"
            "📱 Duplicate links use cached Telegram files\n"
            f"🗑️ Cache expires after {self.config.STORAGE_DAYS}d.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self._menu(u.effective_user.id))
    
    async def help_cmd(self, u, c):
        await u.message.reply_text(
            "📚 Just send an Instagram link to download!\n\n"
            "Commands:\n"
            "/cookies - Upload Instagram cookies (stored in RAM)\n"
            "/start - Main menu\n\n"
            "⚠️ *Privacy Note:* Cookies are stored in RAM only\n"
            "and will be cleared on bot restart or update.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self._menu(u.effective_user.id))
    
    async def cancel_cmd(self, u, c):
        await u.message.reply_text("❌ Cancelled.", reply_markup=self._menu(u.effective_user.id))
        return ConversationHandler.END
    
    async def on_msg(self, u, c):
        uid = u.effective_user.id
        if not self._ok(uid):
            return
        
        url = self._extract_url(u.message.text)
        if not url:
            return
        
        if uid not in self.cookies:
            await u.message.reply_text(
                "❌ Upload Instagram cookies first!\n"
                "Use /cookies command.\n\n"
                "⚠️ Cookies are stored in RAM only\n"
                "and cleared on bot restart.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🍪 Upload Cookies", callback_data='c')
                ]]))
            return
        
        await self._auto_download_and_send(uid, url, u.message)
    
    async def _auto_download_and_send(self, uid, url, msg):
        """Download media and send to Telegram, using cache if available"""
        
        # Check file ID cache first
        cached = self.file_id_cache.get(url)
        if cached and cached.get('file_ids'):
            status = await msg.reply_text("📤 Sending from cache...")
            success = await self._resend_by_file_ids(msg, cached)
            if success:
                await status.delete()
                return
            else:
                await status.edit_text("⚠️ Cache expired, downloading again...")
        
        lock = self._get_download_lock(url)
        
        async with lock:
            cached = self.file_id_cache.get(url)
            if cached and cached.get('file_ids'):
                status = await msg.reply_text("📤 Sending from cache...")
                success = await self._resend_by_file_ids(msg, cached)
                if success:
                    await status.delete()
                    return
                else:
                    await status.edit_text("⚠️ Cache expired, downloading again...")
            
            status = await msg.reply_text("⏳ Downloading...")
            
            try:
                file_paths, title, username = await asyncio.get_event_loop().run_in_executor(
                    None, self._sync_download, uid, url)
                
                file_count = len(file_paths)
                total_size = sum(Path(fp).stat().st_size for fp in file_paths)
                size_mb = total_size / 1024 / 1024
                
                caption = title
                if username:
                    caption = f"📱 @{username}\n{title}"
                if file_count > 1:
                    caption += f"\n\n📸 {file_count} images"
                
                await status.edit_text(f"📤 Uploading {file_count} files ({size_mb:.1f}MB)...")
                
                file_ids = await self._send_media_batch(msg, file_paths, caption)
                
                if file_ids:
                    self.file_id_cache.add(url, file_ids, title, username)
                
                await status.delete()
                
                logger.info(f"Sent {len(file_ids)} files for {url[:80]}")
                
                for fp in file_paths:
                    Path(fp).unlink(missing_ok=True)
                parent = Path(file_paths[0]).parent
                if parent.exists():
                    shutil.rmtree(parent, ignore_errors=True)
                
            except Exception as e:
                logger.error(f"Download error: {str(e)[:200]}")
                await status.edit_text(
                    f"❌ Failed: {str(e)[:200]}\n\n"
                    "Possible reasons:\n"
                    "• Private account (must follow)\n"
                    "• Story expired (24h limit)\n"
                    "• Instagram rate limit\n"
                    "• Invalid or expired cookies\n\n"
                    "⚠️ Re-upload cookies with /cookies if needed",
                    reply_markup=self._menu(uid))
    
    async def _ask_cookies(self, u, c):
        if not self._ok(u.effective_user.id):
            return ConversationHandler.END
        msg = u.callback_query.message if u.callback_query else u.message
        await msg.reply_text(
            "⚠️ *Instagram Cookies Required*\n\n"
            "1️⃣ Login to Instagram in browser\n"
            "2️⃣ Use 'Get cookies.txt LOCALLY' extension\n"
            "3️⃣ Click Export (not Export As JSON)\n"
            "4️⃣ Send the .txt file here\n\n"
            "🔒 *Privacy:* Cookies stored in RAM only\n"
            "🔄 Will be cleared on bot restart/update\n"
            "📁 Never saved to disk",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Cancel", callback_data='b')
            ]]))
        return WAITING_FOR_COOKIES
    
    async def _recv_cookies(self, u, c):
        uid = u.effective_user.id
        if not self._ok(uid):
            return ConversationHandler.END
        
        if not u.message.document:
            await u.message.reply_text("❌ Please send the cookies.txt file.")
            return WAITING_FOR_COOKIES
        
        try:
            # Download cookie file to temp location
            f = await c.bot.get_file(u.message.document.file_id)
            
            # Create temp file (will be auto-deleted if not referenced)
            tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
            tmp_path = tmp.name
            
            await f.download_to_drive(tmp_path)
            
            # Read and validate cookies
            with open(tmp_path, 'r') as cf:
                content = cf.read()
            
            if 'instagram.com' not in content:
                os.unlink(tmp_path)
                await u.message.reply_text(
                    "❌ Invalid cookie file. No Instagram cookies found.\n"
                    "Make sure you're logged into Instagram and export correctly.")
                return WAITING_FOR_COOKIES
            
            # Store in RAM
            self.cookies[uid] = tmp_path
            
            # Clean up old temp file if exists
            # (old file will be garbage collected by OS on restart)
            
            await u.message.reply_text(
                "✅ Cookies loaded into RAM!\n\n"
                "⚠️ *Important:* Cookies are stored in memory only.\n"
                "• They will be cleared if bot restarts\n"
                "• They will be cleared if bot updates\n"
                "• You'll need to re-upload after each restart\n\n"
                "Now send any Instagram link to download.\n"
                "Duplicate links will use cached Telegram files.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self._menu(uid))
            return ConversationHandler.END
            
        except Exception as e:
            logger.error(f"Cookie error: {e}")
            await u.message.reply_text("❌ Failed to process cookies.")
            return WAITING_FOR_COOKIES
    
    async def _cookie_status(self, u, c):
        q = u.callback_query
        await q.answer()
        uid = u.effective_user.id
        
        if uid in self.cookies:
            await q.message.edit_text(
                "✅ Cookies active in RAM\n\n"
                "⚠️ Will be cleared on:\n"
                "• Bot restart\n"
                "• Bot update\n"
                "• Server reboot\n\n"
                "Re-upload with /cookies if needed.",
                reply_markup=self._menu(uid))
        else:
            await q.message.edit_text(
                "❌ No cookies loaded.\n"
                "Use /cookies to upload.",
                reply_markup=self._menu(uid))
    
    async def _router(self, u, c):
        q = u.callback_query
        await q.answer()
        d, uid = q.data, u.effective_user.id
        
        routes = {
            'b': lambda: q.message.edit_text("📋 Menu:", reply_markup=self._menu(uid)),
            'c': lambda: self._ask_cookies(u, c),
            'cookie_status': lambda: self._cookie_status(u, c),
            'cache_info': lambda: q.message.edit_text(
                f"💾 {len(self.file_id_cache._cache)} URLs cached\n"
                f"🗑️ Cache expires after {self.config.STORAGE_DAYS} days\n"
                f"🔄 Duplicate links use cached Telegram files instead of downloading again",
                reply_markup=self._menu(uid)),
        }
        
        if d in routes:
            await routes[d]()
    
    def run(self):
        app = Application.builder().token(self.config.BOT_TOKEN).build()
        
        app.add_handler(CommandHandler('start', self.start_cmd))
        app.add_handler(CommandHandler('help', self.help_cmd))
        app.add_handler(ConversationHandler(
            entry_points=[
                CommandHandler('cookies', self._ask_cookies),
                CallbackQueryHandler(self._ask_cookies, pattern='^c$')
            ],
            states={
                WAITING_FOR_COOKIES: [
                    MessageHandler(filters.Document.FileExtension("txt"), self._recv_cookies),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self._ask_cookies)
                ]
            },
            fallbacks=[
                CommandHandler('cancel', self.cancel_cmd),
                CallbackQueryHandler(self._router, pattern='^b$')
            ],
            per_message=False))
        app.add_handler(CallbackQueryHandler(self._router))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_msg))
        
        logger.info(f"Instagram Bot starting (cookies in RAM, cache: {len(self.file_id_cache._cache)} URLs)...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    InstagramDownloaderBot().run()