#!/usr/bin/env python3
"""
Instagram Downloader Telegram Bot
Uses gallery-dl for reliable Instagram downloads
Auto-downloads and sends media, caches to prevent re-downloads
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
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes
)
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown

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
COOKIES_DIR = DATA_DIR / 'cookies'
DOWNLOADS_DIR = Path('downloads')
CACHE_FILE = DATA_DIR / 'download_cache.json'
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
# Download Cache
# ---------------------------------------------------------------------------
class DownloadCache:
    """Persistent cache to prevent re-downloading same content"""
    
    def __init__(self):
        self._cache: Dict[str, dict] = {}
        self._load()
    
    def _load(self):
        try:
            if CACHE_FILE.exists():
                self._cache = json.loads(CACHE_FILE.read_text())
        except Exception as e:
            logger.error(f"Cache load error: {e}")
            self._cache = {}
    
    def _save(self):
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(self._cache, indent=2))
        except Exception as e:
            logger.error(f"Cache save error: {e}")
    
    def is_cached(self, media_id: str) -> bool:
        entry = self._cache.get(media_id)
        if not entry:
            return False
        file_paths = entry.get('file_paths', [])
        if not file_paths:
            return False
        return all(Path(fp).exists() for fp in file_paths)
    
    def get_cached(self, media_id: str) -> dict:
        return self._cache.get(media_id, {})
    
    def add(self, media_id: str, entry: dict):
        self._cache[media_id] = entry
        self._save()
    
    def remove(self, media_id: str):
        self._cache.pop(media_id, None)
        self._save()
    
    def cleanup_expired(self, days: int):
        cutoff = datetime.now() - timedelta(days=days)
        expired = []
        for media_id, entry in self._cache.items():
            download_time = entry.get('download_time', '')
            try:
                dt = datetime.strptime(download_time, '%Y-%m-%d %H:%M:%S')
                if dt < cutoff:
                    expired.append(media_id)
            except ValueError:
                expired.append(media_id)
        for media_id in expired:
            self.remove(media_id)
        if expired:
            logger.info(f"Cleaned {len(expired)} expired cache entries")

# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
class InstagramDownloaderBot:
    def __init__(self):
        self.config = Config()
        
        for d in (DATA_DIR, COOKIES_DIR, DOWNLOADS_DIR):
            d.mkdir(parents=True, exist_ok=True)
        
        self.cookies: Dict[int, Path] = {}
        self.cache = DownloadCache()
        self._download_tasks: Dict[int, asyncio.Task] = {}
        self._download_locks: Dict[str, asyncio.Lock] = {}
        
        self._check_gallery_dl()
        self._load_cookies()
        self._start_cleanup()
    
    def _check_gallery_dl(self):
        try:
            result = subprocess.run(['gallery-dl', '--version'], capture_output=True, text=True)
            logger.info(f"gallery-dl version: {result.stdout.strip()}")
        except FileNotFoundError:
            logger.error("gallery-dl not found! Installing...")
            subprocess.run([sys.executable, '-m', 'pip', 'install', 'gallery-dl'], check=True)
    
    def _load_cookies(self):
        try:
            fp = DATA_DIR / 'user_cookies.json'
            if fp.exists():
                data = json.loads(fp.read_text())
                self.cookies = {int(k): Path(v) for k, v in data.items()}
        except Exception as e:
            logger.error(f"Load cookies: {e}")
    
    def _save_cookies(self):
        try:
            fp = DATA_DIR / 'user_cookies.json'
            fp.write_text(json.dumps({str(k): str(v) for k, v in self.cookies.items()}, indent=2))
        except Exception as e:
            logger.error(f"Save cookies: {e}")
    
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
        self.cache.cleanup_expired(self.config.STORAGE_DAYS)
    
    def _cookie_path(self, uid):
        return COOKIES_DIR / f'{uid}.txt'
    
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
    
    def _parse_instagram_url(self, url: str) -> Tuple[str, str]:
        if '/p/' in url:
            m = re.search(r'/p/([^/?#]+)', url)
            return ('post', m.group(1) if m else '')
        elif '/reel/' in url:
            m = re.search(r'/reel/([^/?#]+)', url)
            return ('reel', m.group(1) if m else '')
        elif '/stories/' in url:
            m = re.search(r'/stories/([^/?#]+)', url)
            return ('story', m.group(1) if m else 'unknown')
        else:
            m = re.search(r'instagram\.com/([^/?#\s]+)', url)
            return ('profile', m.group(1) if m else 'unknown')
    
    def _get_download_lock(self, media_id: str) -> asyncio.Lock:
        if media_id not in self._download_locks:
            self._download_locks[media_id] = asyncio.Lock()
        return self._download_locks[media_id]
    
    def _get_unique_download_dir(self, uid: int, media_id: str) -> Path:
        timestamp = int(time.time())
        dir_name = f"{uid}_{media_id}_{timestamp}"
        return DOWNLOADS_DIR / dir_name
    
    def _menu(self, uid):
        has = uid in self.cookies
        cache_count = len(self.cache._cache)
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📱 Download History", callback_data='r')],
            [InlineKeyboardButton("🍪 Upload Cookies", callback_data='c')],
            [InlineKeyboardButton(f"🍪 {'✅' if has else '❌'}", callback_data='cs'),
             InlineKeyboardButton(f"💾 {cache_count} cached", callback_data='vc')],
        ])
    
    # --- gallery-dl helpers ---
    def _sync_fetch_info(self, uid, url):
        cookie_path = str(self.cookies[uid])
        
        try:
            cmd = ['gallery-dl', '--cookies', cookie_path, '--dump-json', url]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            if result.returncode != 0:
                raise Exception(f"gallery-dl failed: {result.stderr[:200]}")
            
            data = json.loads(result.stdout)
            
            info = {}
            if isinstance(data, list) and len(data) > 0:
                first = data[0]
                if isinstance(first, list) and len(first) >= 2:
                    meta = first[1]
                    if isinstance(meta, dict):
                        info = {
                            'title': (meta.get('description', '') or 
                                     f"Post by {meta.get('username', 'Unknown')}").strip(),
                            'id': meta.get('post_shortcode', meta.get('post_id', '')),
                            'username': meta.get('username', ''),
                            'fullname': meta.get('fullname', ''),
                            'count': meta.get('count', len(data) - 1),
                            'type': meta.get('type', meta.get('subcategory', 'unknown')),
                        }
            
            if not info:
                raise Exception("Could not parse media info from gallery-dl output")
            
            return info
            
        except subprocess.TimeoutExpired:
            raise Exception("Request timed out")
        except Exception as e:
            logger.error(f"Info fetch error: {e}")
            raise
    
    def _sync_download(self, uid, url, media_id):
        cookie_path = str(self.cookies[uid])
        output_dir = self._get_unique_download_dir(uid, media_id)
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
            
            info = self._sync_fetch_info(uid, url)
            title = info.get('title', 'Instagram Media')
            media_type = info.get('type', 'unknown')
            username = info.get('username', '')
            
            self.cache.add(media_id, {
                'url': url,
                'title': title,
                'username': username,
                'media_type': media_type,
                'file_paths': all_files,
                'file_count': len(all_files),
                'total_size': sum(Path(fp).stat().st_size for fp in all_files),
                'download_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            })
            
            logger.info(f"Downloaded {len(all_files)} files for {media_id}")
            return all_files, title, media_type, username
            
        except Exception as e:
            shutil.rmtree(output_dir, ignore_errors=True)
            raise
    
    # --- Telegram upload helpers ---
    def _split_caption(self, text: str, max_len: int = MAX_CAPTION_LENGTH) -> str:
        if len(text) <= max_len:
            return text
        return text[:max_len - 3] + "..."
    
    async def _send_media_batch(self, msg, file_paths: List[str], caption: str, media_type: str):
        if not file_paths:
            return
        
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
                        await msg.reply_media_group(
                            media=media_group,
                            write_timeout=60,
                            read_timeout=60,
                        )
                        logger.info(f"Sent image batch {batch_idx + 1}/{len(batches)} ({len(batch)} images)")
                    except Exception as e:
                        logger.error(f"Failed to send image batch: {e}")
                        for fp in batch:
                            try:
                                with open(fp, 'rb') as f:
                                    await msg.reply_photo(photo=f)
                            except Exception as e2:
                                logger.error(f"Failed to send individual image: {e2}")
                
                if len(batches) > 1:
                    await asyncio.sleep(1)
        
        for fp in videos:
            try:
                with open(fp, 'rb') as f:
                    await msg.reply_video(
                        video=f,
                        caption=self._split_caption(caption) if not images else None,
                        supports_streaming=True,
                        write_timeout=60,
                    )
            except Exception as e:
                logger.error(f"Failed to send video: {e}")
        
        for fp in others:
            try:
                with open(fp, 'rb') as f:
                    await msg.reply_document(document=f)
            except Exception as e:
                logger.error(f"Failed to send document: {e}")
    
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
            "🍪 Upload cookies first with /cookies\n"
            "📱 View history with /recent\n"
            f"🗑️ Files auto-deleted after {self.config.STORAGE_DAYS}d.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self._menu(u.effective_user.id))
    
    async def help_cmd(self, u, c):
        await u.message.reply_text(
            "📚 Just send an Instagram link to download!\n\n"
            "Commands:\n"
            "/cookies - Upload Instagram cookies\n"
            "/recent - View download history\n"
            "/start - Main menu",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self._menu(u.effective_user.id))
    
    async def recent_cmd(self, u, c):
        await self._show_history(u, c)
    
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
                "Use /cookies command.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🍪 Upload Cookies", callback_data='c')
                ]]))
            return
        
        media_type, media_id = self._parse_instagram_url(url)
        if not media_id:
            await u.message.reply_text("❌ Invalid Instagram URL.")
            return
        
        await self._auto_download_and_send(uid, url, media_type, media_id, u.message)
    
    async def _auto_download_and_send(self, uid, url, media_type, media_id, msg):
        if self.cache.is_cached(media_id):
            cached = self.cache.get_cached(media_id)
            file_paths = cached.get('file_paths', [])
            title = cached.get('title', 'Instagram Media')
            
            if file_paths:
                status = await msg.reply_text(f"📤 Sending from cache: {title[:100]}...")
                await self._send_media_batch(msg, file_paths, title, media_type)
                await status.delete()
                return
        
        lock = self._get_download_lock(media_id)
        
        async with lock:
            if self.cache.is_cached(media_id):
                cached = self.cache.get_cached(media_id)
                file_paths = cached.get('file_paths', [])
                title = cached.get('title', 'Instagram Media')
                
                if file_paths:
                    status = await msg.reply_text(f"📤 Sending from cache: {title[:100]}...")
                    await self._send_media_batch(msg, file_paths, title, media_type)
                    await status.delete()
                    return
            
            status = await msg.reply_text("🔍 Fetching media info...")
            
            try:
                await status.edit_text("⏳ Downloading...")
                
                file_paths, title, dl_type, username = await asyncio.get_event_loop().run_in_executor(
                    None, self._sync_download, uid, url, media_id)
                
                file_count = len(file_paths)
                total_size = sum(Path(fp).stat().st_size for fp in file_paths)
                size_mb = total_size / 1024 / 1024
                
                caption = title
                if username:
                    caption = f"📱 @{username}\n{title}"
                if file_count > 1:
                    caption += f"\n\n📸 {file_count} images"
                
                await status.edit_text(f"📤 Uploading {file_count} files ({size_mb:.1f}MB)...")
                
                await self._send_media_batch(msg, file_paths, caption, dl_type)
                
                await status.delete()
                
                logger.info(f"Successfully sent {file_count} files for {media_id}")
                
                for fp in file_paths:
                    Path(fp).unlink(missing_ok=True)
                parent = Path(file_paths[0]).parent
                if parent.exists():
                    shutil.rmtree(parent, ignore_errors=True)
                
            except Exception as e:
                logger.error(f"Download error for {media_id}: {str(e)[:200]}")
                await status.edit_text(
                    f"❌ Failed: {str(e)[:200]}\n\n"
                    "Possible reasons:\n"
                    "• Private account (must follow)\n"
                    "• Story expired (24h limit)\n"
                    "• Instagram rate limit\n"
                    "• Invalid cookies",
                    reply_markup=self._menu(uid))
    
    async def _show_history(self, u, c, page=0):
        uid = u.effective_user.id
        msg = u.callback_query.message if u.callback_query else u.message
        
        entries = []
        for media_id, entry in self.cache._cache.items():
            entries.append({'media_id': media_id, **entry})
        
        entries.sort(key=lambda x: x.get('download_time', ''), reverse=True)
        
        if not entries:
            await msg.reply_text(
                "📭 No download history.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Menu", callback_data='b')
                ]]))
            return
        
        pp, tp = 5, max(1, (len(entries) + 4) // 5)
        page = max(0, min(page, tp - 1))
        pv = entries[page * pp:(page + 1) * pp]
        
        type_emoji = {
            'post': '🖼️', 'reel': '🎬', 'story': '📖',
            'profile': '🖼️', 'video': '🎬', 'image': '🖼️'
        }
        
        txt = f"📱 *Download History* ({page + 1}/{tp})\n\n"
        for i, e in enumerate(pv, page * pp + 1):
            em = type_emoji.get(e.get('media_type', 'unknown'), '📱')
            file_count = e.get('file_count', 0)
            exists = all(Path(fp).exists() for fp in e.get('file_paths', []))
            ex = "✅" if exists else "🗑️"
            count_str = f" ({file_count} files)" if file_count > 1 else ""
            size_mb = e.get('total_size', 0) / 1024 / 1024
            txt += f"{ex} {em} *{i}.* {escape_markdown(e.get('title', 'Unknown')[:50], version=2)}\n"
            txt += f"   📦 {size_mb:.1f}MB{count_str} | {e.get('download_time', '')}\n\n"
        
        kb = []
        for i, e in enumerate(pv, page * pp + 1):
            em = type_emoji.get(e.get('media_type', 'unknown'), '📱')
            exists = all(Path(fp).exists() for fp in e.get('file_paths', []))
            if exists:
                kb.append([InlineKeyboardButton(
                    f"📤 Resend {em} {i}. {e.get('title', 'Unknown')[:35]}",
                    callback_data=f'resend_{e["media_id"]}')])
        
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f'hist_{page - 1}'))
        if page < tp - 1:
            nav.append(InlineKeyboardButton("➡️", callback_data=f'hist_{page + 1}'))
        if nav:
            kb.append(nav)
        kb.append([InlineKeyboardButton("🗑️ Clear History", callback_data='clear_cache'),
                   InlineKeyboardButton("🔙 Menu", callback_data='b')])
        
        await msg.reply_text(
            txt,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(kb))
    
    async def _resend_from_cache(self, u, c):
        q = u.callback_query
        await q.answer("Resending...")
        media_id = q.data.split('_', 1)[1]
        
        cached = self.cache.get_cached(media_id)
        if not cached:
            await q.message.reply_text("❌ No longer in cache.")
            return
        
        file_paths = cached.get('file_paths', [])
        if not all(Path(fp).exists() for fp in file_paths):
            await q.message.reply_text("❌ Files have been deleted.")
            return
        
        title = cached.get('title', 'Instagram Media')
        media_type = cached.get('media_type', 'unknown')
        
        status = await q.message.reply_text("📤 Resending...")
        await self._send_media_batch(q.message, file_paths, title, media_type)
        await status.delete()
    
    async def _clear_cache(self, u, c):
        q = u.callback_query
        await q.answer()
        
        for entry in self.cache._cache.values():
            for fp in entry.get('file_paths', []):
                Path(fp).unlink(missing_ok=True)
        
        self.cache._cache = {}
        self.cache._save()
        
        await q.message.edit_text(
            "🗑️ All history and files cleared.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Menu", callback_data='b')
            ]]))
    
    async def _ask_cookies(self, u, c):
        if not self._ok(u.effective_user.id):
            return ConversationHandler.END
        msg = u.callback_query.message if u.callback_query else u.message
        await msg.reply_text(
            "⚠️ *Instagram Cookies Required*\n\n"
            "1️⃣ Login to Instagram in browser\n"
            "2️⃣ Use 'Get cookies.txt LOCALLY' extension\n"
            "3️⃣ Click Export (not Export As JSON)\n"
            "4️⃣ Send the .txt file here",
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
            f = await c.bot.get_file(u.message.document.file_id)
            await f.download_to_drive(str(self._cookie_path(uid)))
            self.cookies[uid] = self._cookie_path(uid)
            self._save_cookies()
            await u.message.reply_text(
                "✅ Cookies saved!\n\n"
                "Now send any Instagram link to download.\n"
                "The bot will automatically download and send the media.",
                reply_markup=self._menu(uid))
            return ConversationHandler.END
        except Exception as e:
            logger.error(f"Cookie save error: {e}")
            await u.message.reply_text("❌ Failed to save cookies.")
            return WAITING_FOR_COOKIES
    
    async def _router(self, u, c):
        q = u.callback_query
        await q.answer()
        d, uid = q.data, u.effective_user.id
        
        routes = {
            'b': lambda: q.message.edit_text("📋 Menu:", reply_markup=self._menu(uid)),
            'r': lambda: self._show_history(u, c),
            'c': lambda: self._ask_cookies(u, c),
            'cs': lambda: q.message.edit_text(
                "✅ Cookies ready!" if uid in self.cookies else "❌ Use /cookies",
                reply_markup=self._menu(uid)),
            'vc': lambda: q.message.edit_text(
                f"💾 {len(self.cache._cache)} items in cache\n"
                f"🗑️ Auto-cleanup: {self.config.STORAGE_DAYS} days",
                reply_markup=self._menu(uid)),
            'clear_cache': lambda: self._clear_cache(u, c),
        }
        
        if d in routes:
            await routes[d]()
        elif d.startswith('resend_'):
            await self._resend_from_cache(u, c)
        elif d.startswith('hist_'):
            await self._show_history(u, c, int(d.split('_')[1]))
    
    def run(self):
        app = Application.builder().token(self.config.BOT_TOKEN).build()
        
        app.add_handler(CommandHandler('start', self.start_cmd))
        app.add_handler(CommandHandler('help', self.help_cmd))
        app.add_handler(CommandHandler('recent', self.recent_cmd))
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
        
        logger.info(f"Instagram Bot starting with gallery-dl (cache: {len(self.cache._cache)} items)...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    InstagramDownloaderBot().run()