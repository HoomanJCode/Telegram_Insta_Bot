#!/usr/bin/env python3
"""
Instagram Downloader Telegram Bot
Async file server with aiohttp, non-blocking downloads via thread pool
Supports posts, reels, stories, IGTV, and profile pictures
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
from urllib.parse import quote

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes
)
from telegram.constants import ParseMode
import yt_dlp
from yt_dlp.utils import DownloadError
from aiohttp import web

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
WAITING_FOR_COOKIES = 1

# Instagram URL patterns
INSTAGRAM_RE = re.compile(
    r'(https?://)?(www\.)?instagram\.com/('
    r'p/[^/?#\s]+|'           # Posts
    r'reel/[^/?#\s]+|'        # Reels
    r'tv/[^/?#\s]+|'          # IGTV
    r'stories/[^/?#\s]+|'     # Stories
    r'[^/?#\s]+/?$'           # Profiles
    r')'
)

# Extract media ID from various Instagram URL types
MEDIA_ID_RE = re.compile(r'instagram\.com/(?:p|reel|tv|stories)/([^/?#]+)')
PROFILE_RE = re.compile(r'instagram\.com/([^/?#\s]+)/?$')

# ---------------------------------------------------------------------------
# aiohttp File Server (same as YouTube bot)
# ---------------------------------------------------------------------------
class FileServer:
    def __init__(self, port=8000):
        self.port = port
        self.app = web.Application()
        self.app.router.add_get('/{filename}', self._handle_download)
        self.app.router.add_get('/', self._handle_index)
        self._runner = None
    
    async def _handle_index(self, request):
        files = []
        for f in sorted(DOWNLOADS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.is_file():
                files.append({
                    'name': f.name,
                    'size': f.stat().st_size,
                    'mtime': datetime.fromtimestamp(f.stat().st_mtime).isoformat()
                })
        return web.json_response({'files': files[:50]})
    
    async def _handle_download(self, request):
        filename = request.match_info['filename']
        filepath = DOWNLOADS_DIR / filename
        
        if not filepath.exists() or not filepath.is_file():
            raise web.HTTPNotFound()
        
        response = web.StreamResponse()
        response.headers['Content-Type'] = self._get_mime(filepath.suffix)
        response.headers['Content-Length'] = str(filepath.stat().st_size)
        response.headers['Cache-Control'] = 'public, max-age=86400'
        response.headers['Accept-Ranges'] = 'bytes'
        
        range_header = request.headers.get('Range', '')
        if range_header.startswith('bytes='):
            try:
                range_str = range_header[6:]
                start, end = range_str.split('-')
                start = int(start) if start else 0
                end = int(end) if end else filepath.stat().st_size - 1
                response.set_status(206)
                response.headers['Content-Range'] = f'bytes {start}-{end}/{filepath.stat().st_size}'
                response.headers['Content-Length'] = str(end - start + 1)
            except:
                start, end = 0, filepath.stat().st_size - 1
        else:
            start, end = 0, filepath.stat().st_size - 1
        
        await response.prepare(request)
        
        try:
            with open(filepath, 'rb') as f:
                f.seek(start)
                remaining = end - start + 1
                chunk_size = 1024 * 1024  # 1MB chunks
                while remaining > 0:
                    chunk = f.read(min(chunk_size, remaining))
                    if not chunk:
                        break
                    await response.write(chunk)
                    remaining -= len(chunk)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
        
        return response
    
    def _get_mime(self, ext):
        return {
            '.mp4': 'video/mp4', '.webm': 'video/webm', '.mkv': 'video/x-matroska',
            '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.opus': 'audio/opus',
            '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
            '.webp': 'image/webp', '.gif': 'image/gif',
        }.get(ext.lower(), 'application/octet-stream')
    
    async def start(self):
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, '0.0.0.0', self.port)
        await site.start()
        logger.info("File server on port %d (aiohttp)", self.port)
    
    async def stop(self):
        if self._runner:
            await self._runner.cleanup()

# ---------------------------------------------------------------------------
# Media Record
# ---------------------------------------------------------------------------
class MediaRecord:
    __slots__ = ('title', 'url', 'media_id', 'media_type', 'file_paths',
                 'total_size', 'download_time', 'telegram_file_ids')
    
    def __init__(self, title, url, media_id, media_type, file_paths,
                 total_size, download_time, telegram_file_ids=None):
        self.title = title
        self.url = url
        self.media_id = media_id
        self.media_type = media_type
        self.file_paths = file_paths  # List of paths (for carousels)
        self.total_size = total_size
        self.download_time = download_time
        self.telegram_file_ids = telegram_file_ids or []  # List of file IDs
    
    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}
    
    @classmethod
    def from_dict(cls, d):
        return cls(**d)

# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
class InstagramDownloaderBot:
    def __init__(self):
        self.config = Config()
        self.base_url = self.config.BASE_DOWNLOAD_LINK.rstrip('/')
        try: 
            port = int(self.base_url.split(':')[-1]) if ':' in self.base_url.split('/')[2] else 8000
        except: 
            port = 8000
        
        for d in (DATA_DIR, COOKIES_DIR, DOWNLOADS_DIR):
            d.mkdir(parents=True, exist_ok=True)
        
        self.cookies: Dict[int, Path] = {}
        self.media: Dict[int, List[MediaRecord]] = {}
        self._pending_urls: Dict[int, Tuple[str, str, str, str]] = {}  # uid -> (url, media_id, media_type, title)
        self._download_tasks: Dict[int, asyncio.Task] = {}
        
        self.has_ffmpeg = self._check_ffmpeg()
        self.file_server = FileServer(port=port)
        
        self._load()
        self._start_cleanup()
    
    def _check_ffmpeg(self):
        try:
            subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5)
            return True
        except:
            return False
    
    def _load(self):
        for name, fn, attr in [
            ('cookies', 'user_cookies.json', self.cookies),
            ('media', 'user_media.json', self.media),
        ]:
            try:
                fp = DATA_DIR / fn
                if fp.exists():
                    data = json.loads(fp.read_text())
                    if name == 'media':
                        attr.update({int(k): [MediaRecord.from_dict(v) for v in vs] for k, vs in data.items()})
                    else:
                        attr.update({int(k): Path(v) for k, v in data.items()})
            except Exception as e:
                logger.error("Load %s: %s", name, e)
    
    def _save(self):
        data = {
            DATA_DIR / 'user_cookies.json': {str(k): str(v) for k, v in self.cookies.items()},
            DATA_DIR / 'user_media.json': {str(k): [v.to_dict() for v in vs] for k, vs in self.media.items()},
        }
        for fp, d in data.items():
            try: 
                fp.write_text(json.dumps(d, indent=2))
            except Exception as e: 
                logger.error("Save %s: %s", fp.name, e)
    
    def _start_cleanup(self):
        def w():
            while True:
                try: 
                    self._cleanup()
                except Exception as e: 
                    logger.error("Cleanup: %s", e)
                time.sleep(3600)
        threading.Thread(target=w, daemon=True).start()
    
    def _cleanup(self):
        cutoff = datetime.now() - timedelta(days=self.config.STORAGE_DAYS)
        for f in DOWNLOADS_DIR.iterdir():
            if f.is_file() and datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                f.unlink()
        for uid in list(self.media):
            self.media[uid] = [m for m in self.media[uid] 
                               if any(Path(fp).exists() for fp in m.file_paths)]
            if not self.media[uid]:
                del self.media[uid]
        self._save()
    
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
        """Parse Instagram URL and return (media_type, media_id)"""
        # Post: instagram.com/p/CODE/
        if '/p/' in url:
            m = re.search(r'/p/([^/?#]+)', url)
            return ('post', m.group(1) if m else '')
        
        # Reel: instagram.com/reel/CODE/
        elif '/reel/' in url:
            m = re.search(r'/reel/([^/?#]+)', url)
            return ('reel', m.group(1) if m else '')
        
        # IGTV: instagram.com/tv/CODE/
        elif '/tv/' in url:
            m = re.search(r'/tv/([^/?#]+)', url)
            return ('tv', m.group(1) if m else '')
        
        # Story: instagram.com/stories/USERNAME/STORY_ID/
        elif '/stories/' in url:
            m = re.search(r'/stories/([^/?#]+)', url)
            username = m.group(1) if m else 'unknown'
            story_id = re.search(r'/stories/[^/]+/(\d+)', url)
            return ('story', story_id.group(1) if story_id else username)
        
        # Profile: instagram.com/USERNAME/
        else:
            m = re.search(r'instagram\.com/([^/?#\s]+)', url)
            return ('profile', m.group(1) if m else 'unknown')
    
    def _get_existing_types(self, uid, media_id):
        """Get already downloaded format types for a media ID"""
        types = set()
        for m in self.media.get(uid, []):
            if m.media_id == media_id and any(Path(fp).exists() for fp in m.file_paths):
                types.add(m.media_type)
        return types
    
    def _find_existing(self, uid, media_id, media_type):
        """Find existing download record"""
        for m in self.media.get(uid, []):
            if m.media_id == media_id and m.media_type == media_type:
                if any(Path(fp).exists() for fp in m.file_paths):
                    return m
        return None
    
    @staticmethod
    def _esc(text):
        for c in '*_`[]':
            text = text.replace(c, '\\' + c)
        return text
    
    def _menu(self, uid):
        has = uid in self.cookies
        mc = len(self.media.get(uid, []))
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📱 Recent Downloads", callback_data='r')],
            [InlineKeyboardButton("🍪 Upload Cookies", callback_data='c')],
            [InlineKeyboardButton(f"🍪 {'✅' if has else '❌'}", callback_data='cs'),
             InlineKeyboardButton(f"📦 {mc} files", callback_data='vc')],
        ])
    
    def _format_choice_keyboard(self, uid, media_type, media_id):
        """Build format selection keyboard based on media type"""
        existing = self._get_existing_types(uid, media_id)
        kb = []
        
        if media_type == 'post':
            # Posts can have images (single/carousel) and sometimes video
            img_label = "🖼️ Post Images"
            if 'post_image' in existing:
                img_label = "✅ 🖼️ Post Images - Downloaded"
            kb.append([InlineKeyboardButton(img_label, callback_data='fmt_post_image')])
            
            vid_label = "🎬 Post Video"
            if 'post_video' in existing:
                vid_label = "✅ 🎬 Post Video - Downloaded"
            kb.append([InlineKeyboardButton(vid_label, callback_data='fmt_post_video')])
        
        elif media_type == 'reel':
            label = "🎬 Reel Video"
            if 'reel_video' in existing:
                label = "✅ 🎬 Reel Video - Downloaded"
            kb.append([InlineKeyboardButton(label, callback_data='fmt_reel_video')])
        
        elif media_type == 'tv':
            label = "📺 IGTV Video"
            if 'tv_video' in existing:
                label = "✅ 📺 IGTV Video - Downloaded"
            kb.append([InlineKeyboardButton(label, callback_data='fmt_tv_video')])
        
        elif media_type == 'story':
            img_label = "📸 Story Image"
            if 'story_image' in existing:
                img_label = "✅ 📸 Story Image - Downloaded"
            kb.append([InlineKeyboardButton(img_label, callback_data='fmt_story_image')])
            
            vid_label = "🎥 Story Video"
            if 'story_video' in existing:
                vid_label = "✅ 🎥 Story Video - Downloaded"
            kb.append([InlineKeyboardButton(vid_label, callback_data='fmt_story_video')])
        
        elif media_type == 'profile':
            label = "🖼️ Profile Picture"
            if 'profile_pic' in existing:
                label = "✅ 🖼️ Profile Picture - Downloaded"
            kb.append([InlineKeyboardButton(label, callback_data='fmt_profile_pic')])
        
        kb.append([InlineKeyboardButton("🔙 Cancel", callback_data='b')])
        return InlineKeyboardMarkup(kb)
    
    def _delivery_keyboard(self, uid, idx=None):
        idx_str = str(idx) if idx is not None else 'new'
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Send via Telegram", callback_data=f'tg_{idx_str}')],
            [InlineKeyboardButton("📋 Get Download Link", callback_data=f'lk_{idx_str}')],
            [InlineKeyboardButton("🔙 Back to formats", callback_data=f'backfmt_{idx_str}')],
        ])
    
    # --- Sync helpers (run in executor) ---
    def _sync_fetch_info(self, uid, url):
        """Fetch media info without downloading"""
        opts = {
            'cookiefile': str(self.cookies[uid]),
            'quiet': True, 'no_warnings': True, 'socket_timeout': 30, 'retries': 3,
            'extract_flat': False,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    
    def _sync_download(self, uid, url, media_type):
        """Download Instagram media - returns list of file paths"""
        base_opts = {
            'cookiefile': str(self.cookies[uid]),
            'quiet': True, 'no_warnings': True,
            'socket_timeout': 120, 'retries': 50, 'fragment_retries': 50,
            'no_mtime': True,
            # Instagram requires realistic user-agent
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        }
        
        # Configure format and output template based on media type
        if 'video' in media_type or 'reel' in media_type or 'tv' in media_type:
            # Video formats
            opts = {**base_opts, 
                    'format': 'best[ext=mp4]/best',
                    'outtmpl': str(DOWNLOADS_DIR / '%(id)s_%(format_id)s.%(ext)s'),
                    'merge_output_format': 'mp4'}
        elif 'profile_pic' in media_type:
            # Profile picture only
            opts = {**base_opts,
                    'playlist_items': '0',
                    'skip_download': True,
                    'writethumbnail': True,
                    'outtmpl': str(DOWNLOADS_DIR / '%(id)s_profile.%(ext)s')}
        else:
            # Images (posts, stories)
            opts = {**base_opts,
                    'format': 'best',
                    'outtmpl': str(DOWNLOADS_DIR / '%(id)s_%(format_id)s.%(ext)s')}
        
        # Add rate limiting delay between requests
        import random
        time.sleep(random.uniform(1, 3))  # Random delay to avoid rate limits
        
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get('title', 'Instagram Media')
                vid = info.get('id', '')
                
                downloaded_files = []
                
                # Handle carousel posts (multiple images)
                entries = info.get('entries') or [info]
                for entry in entries:
                    if entry is None:
                        continue
                    # Check requested_downloads for actual downloaded files
                    for rd in entry.get('requested_downloads', []):
                        fp = rd.get('filepath')
                        if fp and Path(fp).exists():
                            downloaded_files.append(fp)
                    
                    # Fallback: check for files by ID pattern
                    if not entry.get('requested_downloads'):
                        for ext in ('.jpg', '.jpeg', '.png', '.webp', '.mp4', '.webm'):
                            pattern = f'{vid}*{ext}'
                            for f in DOWNLOADS_DIR.glob(pattern):
                                if str(f) not in downloaded_files:
                                    downloaded_files.append(str(f))
                
                if not downloaded_files:
                    raise FileNotFoundError(f"No files downloaded for {url}")
                
                return downloaded_files, title, vid
        except Exception as e:
            logger.error("Download failed for %s: %s", url, str(e)[:100])
            raise
    
    # --- Async handlers ---
    async def start_cmd(self, u, c):
        if not self._ok(u.effective_user.id): 
            return
        await u.message.reply_text(
            f"👋 Welcome {u.effective_user.first_name}!\n\n"
            "📱 *Instagram Downloader Bot*\n\n"
            "💡 Supported content:\n"
            "• Posts (images & videos)\n"
            "• Reels\n"
            "• Stories\n"
            "• IGTV\n"
            "• Profile Pictures\n\n"
            "🍪 Upload cookies first with /cookies\n"
            f"🗑️ Files auto-deleted after {self.config.STORAGE_DAYS}d.",
            parse_mode=ParseMode.MARKDOWN, 
            reply_markup=self._menu(u.effective_user.id))
    
    async def help_cmd(self, u, c):
        await u.message.reply_text(
            "📚 Send any Instagram link to download.\n\n"
            "Commands:\n"
            "/cookies - Upload Instagram cookies\n"
            "/recent - View recent downloads\n"
            "/start - Main menu",
            parse_mode=ParseMode.MARKDOWN, 
            reply_markup=self._menu(u.effective_user.id))
    
    async def recent_cmd(self, u, c): 
        await self._show_recent(u, c)
    
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
        
        await self._show_format_choice(uid, url, media_type, media_id, u.message)
    
    async def _show_format_choice(self, uid, url, media_type, media_id, msg):
        s = await msg.reply_text("🔍 Fetching media info...")
        try:
            info = await asyncio.get_event_loop().run_in_executor(
                None, self._sync_fetch_info, uid, url)
            title = info.get('title', 'Instagram Media')
            
            self._pending_urls[uid] = (url, media_id, media_type, title)
            
            existing = self._get_existing_types(uid, media_id)
            dl = ""
            if existing:
                dl = "\n✅ " + " ".join({
                    'post_image': '🖼️', 'post_video': '🎬',
                    'reel_video': '🎬', 'tv_video': '📺',
                    'story_image': '📸', 'story_video': '🎥',
                    'profile_pic': '🖼️'
                }.get(t, '📱') for t in existing)
            
            type_labels = {
                'post': '📷 Post', 'reel': '🎬 Reel',
                'tv': '📺 IGTV', 'story': '📖 Story',
                'profile': '👤 Profile'
            }
            
            await s.edit_text(
                f"{type_labels.get(media_type, '📱')} - *{self._esc(title[:200])}*{dl}\n\n"
                f"Choose format to download:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self._format_choice_keyboard(uid, media_type, media_id))
        except Exception as e:
            logger.error("Info fetch error: %s", str(e)[:100])
            await s.edit_text(
                "❌ Failed to fetch media info.\n"
                "The content may be private or require you to follow the account.",
                reply_markup=self._menu(uid))
    
    async def _choose_format(self, u, c):
        q = u.callback_query
        await q.answer()
        uid, fmt = u.effective_user.id, q.data
        
        if uid not in self._pending_urls:
            await q.message.reply_text("Session expired. Send the link again.")
            return
        
        url, media_id, media_type, title = self._pending_urls[uid]
        
        # Map format keys to download types
        format_map = {
            'fmt_post_image': 'post_image',
            'fmt_post_video': 'post_video',
            'fmt_reel_video': 'reel_video',
            'fmt_tv_video': 'tv_video',
            'fmt_story_image': 'story_image',
            'fmt_story_video': 'story_video',
            'fmt_profile_pic': 'profile_pic',
        }
        
        download_type = format_map.get(fmt)
        if not download_type:
            return
        
        # Check for existing download
        existing = self._find_existing(uid, media_id, download_type)
        if existing:
            await q.answer("Already downloaded!")
            idx = self.media[uid].index(existing)
            await self._show_delivery(q.message, existing, idx)
            return
        
        # Start background download task
        await q.message.edit_text(f"⏳ Downloading {download_type}...\nThis may take a moment.")
        task = asyncio.create_task(self._download_task(uid, url, q.message, download_type, title))
        self._download_tasks[uid] = task
    
    async def _download_task(self, uid, url, msg, download_type, title):
        """Background download task"""
        try:
            file_paths, dl_title, media_id = await asyncio.get_event_loop().run_in_executor(
                None, self._sync_download, uid, url, download_type)
            
            total_size = sum(Path(fp).stat().st_size for fp in file_paths)
            record = MediaRecord(
                title=dl_title or title,
                url=url,
                media_id=media_id,
                media_type=download_type,
                file_paths=file_paths,
                total_size=total_size,
                download_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            )
            
            self.media.setdefault(uid, []).insert(0, record)
            # Keep only last 50 records
            while len(self.media[uid]) > 50:
                old = self.media[uid].pop()
                for fp in old.file_paths:
                    Path(fp).unlink(missing_ok=True)
            self._save()
            
            await self._show_delivery(msg, record, 0)
        except Exception as e:
            logger.error("Download error for uid %d: %s", uid, str(e)[:100])
            await msg.edit_text(
                f"❌ Download failed: {str(e)[:200]}\n\n"
                "Possible reasons:\n"
                "• Private account (must follow)\n"
                "• Story expired (24h limit)\n"
                "• Instagram rate limit\n"
                "• Invalid cookies",
                reply_markup=self._menu(uid))
        finally:
            self._download_tasks.pop(uid, None)
    
    async def _back_to_formats(self, u, c):
        q = u.callback_query
        await q.answer()
        uid = u.effective_user.id
        data = q.data
        
        idx = 0 if 'new' in data else int(data.split('_')[1])
        records = self.media.get(uid, [])
        if idx >= len(records):
            return
        
        record = records[idx]
        media_type, _ = self._parse_instagram_url(record.url)
        self._pending_urls[uid] = (record.url, record.media_id, media_type, record.title)
        
        await q.message.edit_text(
            "Choose format to download:",
            reply_markup=self._format_choice_keyboard(uid, media_type, record.media_id))
    
    async def _show_delivery(self, msg, record, idx):
        """Show delivery options after download"""
        mb = record.total_size / 1024 / 1024
        
        type_labels = {
            'post_image': '🖼️ Post Images',
            'post_video': '🎬 Post Video',
            'reel_video': '🎬 Reel',
            'tv_video': '📺 IGTV',
            'story_image': '📸 Story Image',
            'story_video': '🎥 Story Video',
            'profile_pic': '🖼️ Profile Pic',
        }
        
        file_count = len(record.file_paths)
        count_str = f" ({file_count} files)" if file_count > 1 else ""
        
        await msg.edit_text(
            f"{type_labels.get(record.media_type, '📱')} *{self._esc(record.title[:200])}*\n"
            f"📦 {mb:.2f} MB{count_str} | {record.media_type}\n"
            f"🕒 {record.download_time}\n\n"
            f"Choose delivery method:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self._delivery_keyboard(msg.chat.id, idx))
    
    async def _send_telegram(self, u, c):
        q = u.callback_query
        await q.answer()
        uid = u.effective_user.id
        data = q.data
        
        idx = 0 if 'new' in data else int(data.split('_')[1])
        records = self.media.get(uid, [])
        if idx >= len(records):
            return
        
        record = records[idx]
        
        # Check if files exist
        existing_files = [fp for fp in record.file_paths if Path(fp).exists()]
        if not existing_files:
            await q.message.reply_text("❌ Files have been deleted.")
            return
        
        # Check total size
        total_mb = sum(Path(fp).stat().st_size for fp in existing_files) / 1024 / 1024
        if total_mb > self.config.MAX_TELEGRAM_FILE_SIZE:
            await q.message.reply_text(
                f"⚠️ Total size too large ({total_mb:.1f}MB).\n"
                f"Use download link instead.")
            return
        
        s = await q.message.reply_text("📤 Uploading to Telegram...")
        try:
            sent_ids = []
            for fp in existing_files:
                ext = Path(fp).suffix.lower()
                is_video = ext in ('.mp4', '.webm', '.mkv')
                is_image = ext in ('.jpg', '.jpeg', '.png', '.webp')
                
                with open(fp, 'rb') as f:
                    if is_video:
                        sent = await q.message.reply_video(
                            video=f, 
                            caption=f"{record.title[:200]}",
                            supports_streaming=True)
                        sent_ids.append(sent.video.file_id)
                    elif is_image:
                        sent = await q.message.reply_photo(
                            photo=f,
                            caption=f"{record.title[:200]}")
                        sent_ids.append(sent.photo[-1].file_id)
                    else:
                        sent = await q.message.reply_document(
                            document=f,
                            caption=f"{record.title[:200]}")
                        sent_ids.append(sent.document.file_id)
                
                # Small delay between multiple uploads
                if len(existing_files) > 1:
                    await asyncio.sleep(0.5)
            
            record.telegram_file_ids = sent_ids
            self._save()
            await s.delete()
            await q.message.delete()
        except Exception as e:
            logger.error("Upload error: %s", str(e)[:100])
            await s.edit_text("❌ Upload failed. Try download link instead.")
    
    async def _send_link(self, u, c):
        q = u.callback_query
        await q.answer()
        uid = u.effective_user.id
        data = q.data
        
        idx = 0 if 'new' in data else int(data.split('_')[1])
        records = self.media.get(uid, [])
        if idx >= len(records):
            return
        
        record = records[idx]
        existing_files = [fp for fp in record.file_paths if Path(fp).exists()]
        
        if not existing_files:
            await q.message.reply_text("❌ Files have been deleted.")
            return
        
        links = []
        for fp in existing_files:
            url = f"{self.base_url}/{quote(Path(fp).name)}"
            links.append(f"📥 `{url}`")
        
        await q.message.edit_text(
            "📋 *Download Links*\n\n" + "\n".join(links) + 
            f"\n\n⚠️ Links expire after {self.config.STORAGE_DAYS} days.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Menu", callback_data='b')
            ]]))
    
    async def _show_recent(self, u, c, page=0):
        uid = u.effective_user.id
        msg = u.callback_query.message if u.callback_query else u.message
        media_list = self.media.get(uid, [])
        
        if not media_list:
            await msg.reply_text(
                "📭 No recent downloads.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Menu", callback_data='b')
                ]]))
            return
        
        pp, tp = 5, max(1, (len(media_list) + 4) // 5)
        page = max(0, min(page, tp - 1))
        pv = media_list[page * pp:(page + 1) * pp]
        
        type_emoji = {
            'post_image': '🖼️', 'post_video': '🎬',
            'reel_video': '🎬', 'tv_video': '📺',
            'story_image': '📸', 'story_video': '🎥',
            'profile_pic': '🖼️'
        }
        
        txt = f"📱 *Recent Downloads* ({page + 1}/{tp})\n\n"
        for i, m in enumerate(pv, page * pp + 1):
            ex = "✅" if any(Path(fp).exists() for fp in m.file_paths) else "🗑️"
            em = type_emoji.get(m.media_type, '📱')
            file_count = len(m.file_paths)
            count_str = f" ({file_count} files)" if file_count > 1 else ""
            txt += f"{ex} {em} *{i}.* {self._esc(m.title[:50])}\n"
            txt += f"   📦 {m.total_size / 1024 / 1024:.2f}MB{count_str} | {m.download_time}\n\n"
        
        kb = []
        for i, m in enumerate(pv, page * pp + 1):
            if any(Path(fp).exists() for fp in m.file_paths):
                em = type_emoji.get(m.media_type, '📱')
                kb.append([InlineKeyboardButton(
                    f"{em} {i}. {m.title[:40]}",
                    callback_data=f'sel_{page * pp + (i - page * pp - 1)}')])
        
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f'p_{page - 1}'))
        if page < tp - 1:
            nav.append(InlineKeyboardButton("➡️", callback_data=f'p_{page + 1}'))
        if nav:
            kb.append(nav)
        kb.append([InlineKeyboardButton("🔙 Menu", callback_data='b')])
        
        await msg.reply_text(
            txt, 
            parse_mode=ParseMode.MARKDOWN, 
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(kb))
    
    async def _select_media(self, u, c):
        q = u.callback_query
        await q.answer()
        uid, idx = u.effective_user.id, int(q.data.split('_')[1])
        media_list = self.media.get(uid, [])
        if 0 <= idx < len(media_list):
            await self._show_delivery(q.message, media_list[idx], idx)
    
    async def _delete_media(self, u, c):
        q = u.callback_query
        await q.answer()
        uid, idx = u.effective_user.id, int(q.data.split('_')[1])
        media_list = self.media.get(uid, [])
        if 0 <= idx < len(media_list):
            record = media_list[idx]
            for fp in record.file_paths:
                Path(fp).unlink(missing_ok=True)
            media_list.pop(idx)
            self._save()
            await q.message.reply_text(
                "🗑️ Deleted.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📱 Recent", callback_data='r'),
                    InlineKeyboardButton("🔙 Menu", callback_data='b')
                ]]))
    
    async def _ask_cookies(self, u, c):
        if not self._ok(u.effective_user.id):
            return ConversationHandler.END
        msg = u.callback_query.message if u.callback_query else u.message
        await msg.reply_text(
            "⚠️ *Instagram Cookies Required*\n\n"
            "1️⃣ Login to Instagram in your browser\n"
            "2️⃣ Use 'Get cookies.txt LOCALLY' extension\n"
            "3️⃣ Export cookies.txt\n"
            "4️⃣ Send the .txt file here\n\n"
            "⚠️ *Security*: Cookies contain session tokens.\n"
            "Keep them safe and never share!",
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
            await u.message.reply_text(
                "❌ Please send the cookies.txt file.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Cancel", callback_data='b')
                ]]))
            return WAITING_FOR_COOKIES
        
        try:
            f = await c.bot.get_file(u.message.document.file_id)
            await f.download_to_drive(str(self._cookie_path(uid)))
            self.cookies[uid] = self._cookie_path(uid)
            self._save()
            await u.message.reply_text(
                "✅ Cookies saved!\n\n"
                "Now send any Instagram link to download.\n\n"
                "Supported:\n"
                "• Posts (images & videos)\n"
                "• Reels\n"
                "• Stories\n"
                "• IGTV\n"
                "• Profile Pictures",
                reply_markup=self._menu(uid))
            return ConversationHandler.END
        except Exception as e:
            logger.error("Cookie save error for uid %d: %s", uid, e)
            await u.message.reply_text("❌ Failed to save cookies. Try again.")
            return WAITING_FOR_COOKIES
    
    async def _router(self, u, c):
        q = u.callback_query
        await q.answer()
        d, uid = q.data, u.effective_user.id
        
        routes = {
            'b': lambda: q.message.edit_text(
                "📋 Main Menu:", reply_markup=self._menu(uid)),
            'r': lambda: self._show_recent(u, c),
            'c': lambda: self._ask_cookies(u, c),
            'cs': lambda: q.message.edit_text(
                "✅ Cookies ready!" if uid in self.cookies else "❌ Use /cookies to upload.",
                reply_markup=self._menu(uid)),
            'vc': lambda: q.message.edit_text(
                f"📦 {len(self.media.get(uid, []))} downloaded items",
                reply_markup=self._menu(uid)),
        }
        
        if d in routes:
            await routes[d]()
        elif d.startswith('fmt_'):
            await self._choose_format(u, c)
        elif d.startswith('backfmt_'):
            await self._back_to_formats(u, c)
        elif d.startswith('tg_'):
            await self._send_telegram(u, c)
        elif d.startswith('lk_'):
            await self._send_link(u, c)
        elif d.startswith('sel_'):
            await self._select_media(u, c)
        elif d.startswith('d_'):
            await self._delete_media(u, c)
        elif d.startswith('p_'):
            await self._show_recent(u, c, int(d.split('_')[1]))
    
    # --- Run ---
    async def _start_file_server(self):
        await self.file_server.start()
    
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
        
        # Start file server alongside bot
        loop = asyncio.get_event_loop()
        loop.create_task(self._start_file_server())
        
        logger.info("Instagram Bot starting with aiohttp file server...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    InstagramDownloaderBot().run()