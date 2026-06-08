#!/usr/bin/env python3
"""
Instagram Downloader Telegram Bot
Uses gallery-dl for reliable Instagram downloads
Auto-downloads and sends media, caches Telegram file IDs to prevent re-uploads
Supports inline mode - use @botname <link> in any chat
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
from uuid import uuid4

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto,
    InlineQueryResultArticle, InputTextMessageContent
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes, InlineQueryHandler
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
COOKIE_IDS_FILE = DATA_DIR / 'cookie_file_ids.json'
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
INLINE_TRIGGER_PREFIX = "📥 "

# ---------------------------------------------------------------------------
# File ID Cache
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
        
        self.cookies: Dict[int, str] = {}
        self.cookie_file_ids: Dict[int, str] = {}
        
        self.file_id_cache = FileIDCache(self.config.STORAGE_DAYS)
        self._download_locks: Dict[str, asyncio.Lock] = {}
        self._cookie_locks: Dict[int, asyncio.Lock] = {}
        
        self._check_gallery_dl()
        self._load_cookie_ids()
        self._start_cleanup()
    
    def _check_gallery_dl(self):
        try:
            result = subprocess.run(['gallery-dl', '--version'], capture_output=True, text=True)
            logger.info(f"gallery-dl version: {result.stdout.strip()}")
        except FileNotFoundError:
            logger.error("gallery-dl not found! Installing...")
            subprocess.run([sys.executable, '-m', 'pip', 'install', 'gallery-dl'], check=True)
    
    def _load_cookie_ids(self):
        try:
            if COOKIE_IDS_FILE.exists():
                data = json.loads(COOKIE_IDS_FILE.read_text())
                self.cookie_file_ids = {int(k): v for k, v in data.items()}
                logger.info(f"Loaded {len(self.cookie_file_ids)} cookie file references")
        except Exception as e:
            logger.error(f"Load cookie IDs error: {e}")
            self.cookie_file_ids = {}
    
    def _save_cookie_ids(self):
        try:
            COOKIE_IDS_FILE.parent.mkdir(parents=True, exist_ok=True)
            COOKIE_IDS_FILE.write_text(json.dumps(
                {str(k): v for k, v in self.cookie_file_ids.items()}, indent=2))
        except Exception as e:
            logger.error(f"Save cookie IDs error: {e}")
    
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
    
    def _is_admin(self, uid):
        admins = self.config.get_admins()
        if not admins:
            return False
        return uid in admins
    
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
    
    def _get_cookie_lock(self, uid: int) -> asyncio.Lock:
        if uid not in self._cookie_locks:
            self._cookie_locks[uid] = asyncio.Lock()
        return self._cookie_locks[uid]
    
    def _get_unique_download_dir(self, uid: int) -> Path:
        timestamp = int(time.time())
        dir_name = f"{uid}_{timestamp}"
        return DOWNLOADS_DIR / dir_name
    
    def _menu(self, uid):
        has_cookies = uid in self.cookies
        has_cookie_id = uid in self.cookie_file_ids
        cookie_status = "✅" if has_cookies else ("📎" if has_cookie_id else "❌")
        
        buttons = [[InlineKeyboardButton("🍪 Upload Cookies", callback_data='c')]]
        
        if self._is_admin(uid):
            cache_count = len(self.file_id_cache._cache)
            buttons.append([
                InlineKeyboardButton(f"🍪 {cookie_status} Cookies", callback_data='cookie_status'),
                InlineKeyboardButton(f"💾 {cache_count} cached", callback_data='cache_info'),
            ])
        else:
            buttons.append([
                InlineKeyboardButton(f"🍪 {cookie_status} Cookies", callback_data='cookie_status'),
            ])
        
        return InlineKeyboardMarkup(buttons)
    
    async def _ensure_cookies_loaded(self, uid: int, context) -> bool:
        if uid in self.cookies:
            return True
        if uid not in self.cookie_file_ids:
            return False
        
        lock = self._get_cookie_lock(uid)
        async with lock:
            if uid in self.cookies:
                return True
            try:
                file_id = self.cookie_file_ids[uid]
                tg_file = await context.bot.get_file(file_id)
                tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
                tmp_path = tmp.name
                await tg_file.download_to_drive(tmp_path)
                
                with open(tmp_path, 'r') as f:
                    content = f.read()
                if 'instagram.com' not in content:
                    os.unlink(tmp_path)
                    logger.warning(f"Downloaded cookie file for {uid} is invalid")
                    return False
                
                self.cookies[uid] = tmp_path
                logger.info(f"Cookies loaded from Telegram for user {uid}")
                return True
            except Exception as e:
                logger.error(f"Failed to download cookies for {uid}: {e}")
                return False
    
    def _sync_download(self, uid, url):
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
    
    def _split_caption(self, text: str, max_len: int = MAX_CAPTION_LENGTH) -> str:
        if len(text) <= max_len:
            return text
        return text[:max_len - 3] + "..."
    
    async def _send_media_batch(self, chat_id, context, file_paths: List[str], caption: str):
        """Send media files to Telegram chat, return file IDs"""
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
                        sent = await context.bot.send_media_group(
                            chat_id=chat_id,
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
                                    s = await context.bot.send_photo(
                                        chat_id=chat_id, photo=f)
                                    all_file_ids.append(s.photo[-1].file_id)
                            except Exception as e2:
                                logger.error(f"Failed to send individual image: {e2}")
                
                if len(batches) > 1:
                    await asyncio.sleep(1)
        
        for fp in videos:
            try:
                with open(fp, 'rb') as f:
                    s = await context.bot.send_video(
                        chat_id=chat_id,
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
                    s = await context.bot.send_document(chat_id=chat_id, document=f)
                    all_file_ids.append(s.document.file_id)
            except Exception as e:
                logger.error(f"Failed to send document: {e}")
        
        return all_file_ids
    
    async def _resend_by_file_ids(self, chat_id, context, cached_entry: dict):
        """Resend media using cached Telegram file IDs to a specific chat"""
        file_ids = cached_entry.get('file_ids', [])
        title = cached_entry.get('title', '')
        
        if not file_ids:
            return False
        
        try:
            for i, file_id in enumerate(file_ids):
                caption = self._split_caption(title) if i == 0 and title else None
                try:
                    await context.bot.send_video(
                        chat_id=chat_id, video=file_id,
                        caption=caption, supports_streaming=True)
                except:
                    try:
                        await context.bot.send_photo(
                            chat_id=chat_id, photo=file_id, caption=caption)
                    except:
                        try:
                            await context.bot.send_document(
                                chat_id=chat_id, document=file_id, caption=caption)
                        except:
                            return False
                await asyncio.sleep(0.3)
            return True
        except Exception as e:
            logger.error(f"Resend error: {e}")
            return False
    
    # --- Handlers ---
    async def start_cmd(self, u, c):
        if not self._ok(u.effective_user.id):
            return
        
        uid = u.effective_user.id
        has_cookies = uid in self.cookies
        has_cookie_id = uid in self.cookie_file_ids
        
        cookie_info = ""
        if has_cookies:
            cookie_info = "\n✅ Cookies active in RAM"
        elif has_cookie_id:
            cookie_info = "\n📎 Cookie reference saved - loads automatically"
        else:
            cookie_info = "\n❌ No cookies - upload with /cookies"
        
        await u.message.reply_text(
            f"👋 Welcome {u.effective_user.first_name}!\n\n"
            "📱 *Instagram Downloader Bot*\n\n"
            "💡 Just send an Instagram link!\n"
            "• Posts → All images/videos\n"
            "• Reels → Video\n"
            "• Stories → Images/videos\n"
            "• Profiles → Profile picture\n\n"
            "🌐 *Inline Mode:*\n"
            "Type @botname <link> in any chat!\n\n"
            f"{cookie_info}\n"
            "🔄 Duplicate links use cached Telegram files\n"
            f"🗑️ Cache expires after {self.config.STORAGE_DAYS}d.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self._menu(uid))
    
    async def help_cmd(self, u, c):
        await u.message.reply_text(
            "📚 Just send an Instagram link to download!\n\n"
            "Commands:\n"
            "/cookies - Upload Instagram cookies\n"
            "/start - Main menu\n\n"
            "🌐 *Inline Mode:* Type @botname <link> in any chat\n"
            "to share Instagram media instantly.\n\n"
            "🔒 Bot stores only a file reference to reload\n"
            "cookies on restart, not the cookies themselves.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self._menu(u.effective_user.id))
    
    async def cancel_cmd(self, u, c):
        await u.message.reply_text("❌ Cancelled.", reply_markup=self._menu(u.effective_user.id))
        return ConversationHandler.END
    
    async def on_msg(self, u, c):
        uid = u.effective_user.id
        if not self._ok(uid):
            return
        
        if u.message.text and u.message.text.startswith(INLINE_TRIGGER_PREFIX):
            return
        
        url = self._extract_url(u.message.text)
        if not url:
            return
        
        if uid not in self.cookies:
            loaded = await self._ensure_cookies_loaded(uid, c)
            if not loaded:
                await u.message.reply_text(
                    "❌ Cookies not available.\n"
                    "Use /cookies to upload your Instagram cookies.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🍪 Upload Cookies", callback_data='c')
                    ]]))
                return
        
        await self._auto_download_and_send(uid, url, u.message.chat_id, c)
    
    async def _auto_download_and_send(self, uid, url, chat_id, context):
        """Download media and send to Telegram chat, using cache if available"""
        
        cached = self.file_id_cache.get(url)
        if cached and cached.get('file_ids'):
            status = await context.bot.send_message(chat_id=chat_id, text="📤 Sending from cache...")
            success = await self._resend_by_file_ids(chat_id, context, cached)
            if success:
                await status.delete()
                return
            else:
                await status.edit_text("⚠️ Cache expired, downloading again...")
        
        lock = self._get_download_lock(url)
        
        async with lock:
            cached = self.file_id_cache.get(url)
            if cached and cached.get('file_ids'):
                status = await context.bot.send_message(chat_id=chat_id, text="📤 Sending from cache...")
                success = await self._resend_by_file_ids(chat_id, context, cached)
                if success:
                    await status.delete()
                    return
                else:
                    await status.edit_text("⚠️ Cache expired, downloading again...")
            
            status = await context.bot.send_message(chat_id=chat_id, text="⏳ Downloading...")
            
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
                
                file_ids = await self._send_media_batch(chat_id, context, file_paths, caption)
                
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
                    "⚠️ Re-upload cookies with /cookies if needed")
    
    async def inline_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.inline_query.query.strip()
        uid = update.inline_query.from_user.id
        
        if not self._ok(uid):
            await update.inline_query.answer([], switch_pm_text="❌ Not authorized", switch_pm_parameter="start")
            return
        
        url = self._extract_url(query)
        if not url:
            results = [
                InlineQueryResultArticle(
                    id=str(uuid4()),
                    title="Send an Instagram link",
                    description="Example: https://www.instagram.com/p/CODE/",
                    input_message_content=InputTextMessageContent(
                        "📱 Send an Instagram link to download."
                    )
                )
            ]
            await update.inline_query.answer(results, cache_time=10)
            return
        
        cached = self.file_id_cache.get(url)
        if cached:
            file_ids = cached.get('file_ids', [])
            title = cached.get('title', 'Instagram Media')
            
            results = [
                InlineQueryResultArticle(
                    id=str(uuid4()),
                    title=f"📱 {title[:50]}",
                    description=f"Tap to send from cache ({len(file_ids)} files)",
                    input_message_content=InputTextMessageContent(
                        f"{INLINE_TRIGGER_PREFIX}{url}"
                    )
                )
            ]
            await update.inline_query.answer(results, cache_time=30)
            return
        
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="📥 Download Instagram media",
                description=f"Download from {url[:60]}",
                input_message_content=InputTextMessageContent(
                    f"{INLINE_TRIGGER_PREFIX}{url}"
                )
            )
        ]
        await update.inline_query.answer(results, cache_time=10)
    
    async def handle_inline_trigger(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.message
        if not msg or not msg.text:
            return
        if not msg.text.startswith(INLINE_TRIGGER_PREFIX):
            return
        
        uid = msg.from_user.id
        if not self._ok(uid):
            return
        
        url = msg.text[len(INLINE_TRIGGER_PREFIX):].strip()
        if not self._extract_url(url):
            return
        
        if uid not in self.cookies:
            loaded = await self._ensure_cookies_loaded(uid, context)
            if not loaded:
                await msg.reply_text("❌ Cookies not available. Please set them in private chat with /cookies.")
                return
        
        await self._auto_download_and_send(uid, url, msg.chat_id, context)
    
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
            "🔒 Cookie content stays in RAM only\n"
            "📎 Bot stores only a file reference, not your cookies",
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
            doc = u.message.document
            tg_file = await c.bot.get_file(doc.file_id)
            tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
            tmp_path = tmp.name
            await tg_file.download_to_drive(tmp_path)
            
            with open(tmp_path, 'r') as f:
                content = f.read()
            
            if 'instagram.com' not in content:
                os.unlink(tmp_path)
                await u.message.reply_text(
                    "❌ Invalid cookie file. No Instagram cookies found.\n"
                    "Make sure you're logged into Instagram and export correctly.")
                return WAITING_FOR_COOKIES
            
            self.cookies[uid] = tmp_path
            self.cookie_file_ids[uid] = doc.file_id
            self._save_cookie_ids()
            
            await u.message.reply_text(
                "✅ Cookies saved!\n\n"
                "🔒 Cookie content stored in RAM only\n"
                "📎 File reference saved for auto-reload on restart\n"
                "🔄 Cookies auto-load when bot restarts\n\n"
                "Now send any Instagram link to download.\n"
                "Duplicate links will use cached Telegram files.\n\n"
                "🌐 *Inline Mode:* Type @botname <link> in any chat!",
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
        
        in_ram = uid in self.cookies
        on_disk = uid in self.cookie_file_ids
        
        if in_ram:
            status = "✅ Cookies active in RAM\n"
        elif on_disk:
            status = "📎 Cookie reference saved (loads automatically)\n"
        else:
            status = "❌ No cookies\n"
        
        status += "\n🔒 *How it works:*\n"
        status += "• Cookie content: RAM only\n"
        status += "• Bot stores: Only a file reference\n"
        status += "• File reference works only with this bot\n"
        status += "• No cookie data written to disk"
        
        if self._is_admin(uid):
            status += f"\n\n📊 *Admin:* {len(self.file_id_cache._cache)} URLs cached"
        
        await q.message.edit_text(status, parse_mode=ParseMode.MARKDOWN, reply_markup=self._menu(uid))
    
    async def _show_cache_info(self, u, c):
        q = u.callback_query
        uid = u.effective_user.id
        
        if not self._is_admin(uid):
            await q.answer("Admin only", show_alert=True)
            return
        
        await q.message.edit_text(
            f"💾 {len(self.file_id_cache._cache)} URLs cached\n"
            f"🗑️ Cache expires after {self.config.STORAGE_DAYS} days\n"
            f"🔄 Duplicate links use cached Telegram files\n"
            f"📎 Cookie references saved for {len(self.cookie_file_ids)} users\n\n"
            f"ℹ️ Bot stores only Telegram file references,\n"
            f"not actual cookie data.",
            reply_markup=self._menu(uid))
    
    async def _router(self, u, c):
        q = u.callback_query
        await q.answer()
        d, uid = q.data, u.effective_user.id
        
        routes = {
            'b': lambda: q.message.edit_text("📋 Menu:", reply_markup=self._menu(uid)),
            'c': lambda: self._ask_cookies(u, c),
            'cookie_status': lambda: self._cookie_status(u, c),
            'cache_info': lambda: self._show_cache_info(u, c),
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
        app.add_handler(MessageHandler(
            filters.TEXT & filters.Regex(f'^{re.escape(INLINE_TRIGGER_PREFIX)}'),
            self.handle_inline_trigger
        ))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_msg))
        app.add_handler(InlineQueryHandler(self.inline_query))
        
        logger.info(f"Instagram Bot starting (inline mode, cookie refs: {len(self.cookie_file_ids)}, cache: {len(self.file_id_cache._cache)})...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    InstagramDownloaderBot().run()