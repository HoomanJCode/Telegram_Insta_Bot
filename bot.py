#!/usr/bin/env python3
"""
Instagram Downloader Telegram Bot
Downloads Instagram content and uploads directly to Telegram
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
import socket
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes
)
from telegram.constants import ParseMode
import yt_dlp
from yt_dlp.utils import DownloadError

from config import Config

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.WARNING)
for lib in ('httpx', 'httpcore', 'telegram', 'telegram.ext', 'aiohttp'):
    logging.getLogger(lib).setLevel(logging.WARNING)

logger = logging.getLogger('ig_bot')
logger.setLevel(logging.DEBUG)  # Enable debug logging
h = logging.StreamHandler()
h.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(h)
logger.propagate = False

# Also log to file for persistent debugging
fh = logging.FileHandler('debug.log')
fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(fh)

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
        self.file_paths = file_paths
        self.total_size = total_size
        self.download_time = download_time
        self.telegram_file_ids = telegram_file_ids or []
    
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
        
        for d in (DATA_DIR, COOKIES_DIR, DOWNLOADS_DIR):
            d.mkdir(parents=True, exist_ok=True)
        
        self.cookies: Dict[int, Path] = {}
        self.media: Dict[int, List[MediaRecord]] = {}
        self._pending_urls: Dict[int, Tuple[str, str, str, str]] = {}
        self._download_tasks: Dict[int, asyncio.Task] = {}
        
        self._load()
        self._start_cleanup()
    
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
        if '/p/' in url:
            m = re.search(r'/p/([^/?#]+)', url)
            return ('post', m.group(1) if m else '')
        elif '/reel/' in url:
            m = re.search(r'/reel/([^/?#]+)', url)
            return ('reel', m.group(1) if m else '')
        elif '/tv/' in url:
            m = re.search(r'/tv/([^/?#]+)', url)
            return ('tv', m.group(1) if m else '')
        elif '/stories/' in url:
            m = re.search(r'/stories/([^/?#]+)', url)
            username = m.group(1) if m else 'unknown'
            story_id = re.search(r'/stories/[^/]+/(\d+)', url)
            return ('story', story_id.group(1) if story_id else username)
        else:
            m = re.search(r'instagram\.com/([^/?#\s]+)', url)
            return ('profile', m.group(1) if m else 'unknown')
    
    def _get_existing_types(self, uid, media_id):
        types = set()
        for m in self.media.get(uid, []):
            if m.media_id == media_id and any(Path(fp).exists() for fp in m.file_paths):
                types.add(m.media_type)
        return types
    
    def _find_existing(self, uid, media_id, media_type):
        for m in self.media.get(uid, []):
            if m.media_id == media_id and m.media_type == media_type:
                if any(Path(fp).exists() for fp in m.file_paths):
                    return m
        return None
    
    def _validate_cookies(self, uid):
        """Check if cookies are valid by testing essential cookies"""
        cookie_path = self._cookie_path(uid)
        if not cookie_path.exists():
            logger.debug(f"Cookie file does not exist: {cookie_path}")
            return False
        
        try:
            content = cookie_path.read_text()
            logger.debug(f"Cookie file size: {len(content)} bytes")
            
            if 'instagram.com' not in content:
                logger.debug("No instagram.com entries in cookie file")
                return False
            
            essential = ['sessionid', 'ds_user_id', 'csrftoken']
            found = []
            missing = []
            for cookie in essential:
                if cookie in content:
                    found.append(cookie)
                else:
                    missing.append(cookie)
            
            logger.debug(f"Found cookies: {found}")
            logger.debug(f"Missing cookies: {missing}")
            
            return len(found) > 0  # At least some essential cookies found
        except Exception as e:
            logger.error(f"Cookie validation error: {e}")
            return False
    
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
            [InlineKeyboardButton("🔍 Debug Cookies", callback_data='debug')],
            [InlineKeyboardButton(f"🍪 {'✅' if has else '❌'}", callback_data='cs'),
             InlineKeyboardButton(f"📦 {mc} files", callback_data='vc')],
        ])
    
    def _format_choice_keyboard(self, uid, media_type, media_id):
        existing = self._get_existing_types(uid, media_id)
        kb = []
        
        if media_type == 'post':
            img_label = "🖼️ Post Images"
            if 'post_image' in existing:
                img_label = "✅ 🖼️ Post Images"
            kb.append([InlineKeyboardButton(img_label, callback_data='fmt_post_image')])
            
            vid_label = "🎬 Post Video"
            if 'post_video' in existing:
                vid_label = "✅ 🎬 Post Video"
            kb.append([InlineKeyboardButton(vid_label, callback_data='fmt_post_video')])
        
        elif media_type == 'reel':
            label = "🎬 Reel Video"
            if 'reel_video' in existing:
                label = "✅ 🎬 Reel Video"
            kb.append([InlineKeyboardButton(label, callback_data='fmt_reel_video')])
        
        elif media_type == 'tv':
            label = "📺 IGTV Video"
            if 'tv_video' in existing:
                label = "✅ 📺 IGTV Video"
            kb.append([InlineKeyboardButton(label, callback_data='fmt_tv_video')])
        
        elif media_type == 'story':
            img_label = "📸 Story Image"
            if 'story_image' in existing:
                img_label = "✅ 📸 Story Image"
            kb.append([InlineKeyboardButton(img_label, callback_data='fmt_story_image')])
            
            vid_label = "🎥 Story Video"
            if 'story_video' in existing:
                vid_label = "✅ 🎥 Story Video"
            kb.append([InlineKeyboardButton(vid_label, callback_data='fmt_story_video')])
        
        elif media_type == 'profile':
            label = "🖼️ Profile Picture"
            if 'profile_pic' in existing:
                label = "✅ 🖼️ Profile Picture"
            kb.append([InlineKeyboardButton(label, callback_data='fmt_profile_pic')])
        
        kb.append([InlineKeyboardButton("🔙 Cancel", callback_data='b')])
        return InlineKeyboardMarkup(kb)
    
    # --- Sync helpers (run in executor) ---
    def _sync_fetch_info(self, uid, url):
        """Fetch media info with debug logging"""
        cookie_path = str(self.cookies[uid])
        
        # Debug: Check cookie file
        logger.info(f"=== DEBUG: Cookie file: {cookie_path}")
        logger.info(f"=== DEBUG: Cookie exists: {Path(cookie_path).exists()}")
        if Path(cookie_path).exists():
            cookie_content = Path(cookie_path).read_text()
            logger.info(f"=== DEBUG: Cookie file size: {len(cookie_content)} bytes")
            # Show first few lines for debugging
            lines = cookie_content.split('\n')[:10]
            logger.info(f"=== DEBUG: First 10 lines of cookie file:")
            for i, line in enumerate(lines):
                logger.info(f"  Line {i}: {line[:200]}")
            
            # Check for essential cookies
            essential = ['sessionid', 'ds_user_id', 'csrftoken', 'mid', 'ig_did']
            for cookie in essential:
                if cookie in cookie_content:
                    # Extract the value (show partial)
                    idx = cookie_content.find(cookie)
                    snippet = cookie_content[idx:idx+100]
                    logger.info(f"  Found {cookie}: {snippet[:80]}...")
                else:
                    logger.warning(f"  Missing essential cookie: {cookie}")
        
        logger.info(f"=== DEBUG: Fetching URL: {url}")
        
        try:
            # First try with subprocess for better error messages
            logger.info("=== DEBUG: Attempting with subprocess yt-dlp")
            cmd = [
                'yt-dlp',
                '--cookies', cookie_path,
                '--dump-json',
                '--no-download',
                '--verbose',
                '--no-check-certificate',
                '--add-header', 'User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                '--add-header', 'Accept:text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                '--add-header', 'Accept-Language:en-US,en;q=0.9',
                url
            ]
            
            logger.info(f"=== DEBUG: Running: {' '.join(cmd)}")
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            logger.info(f"=== DEBUG: Return code: {result.returncode}")
            
            if result.stdout:
                logger.info(f"=== DEBUG: STDOUT (first 2000 chars):\n{result.stdout[:2000]}")
            if result.stderr:
                logger.info(f"=== DEBUG: STDERR (first 2000 chars):\n{result.stderr[:2000]}")
            
            if result.returncode == 0 and result.stdout:
                try:
                    info = json.loads(result.stdout)
                    logger.info(f"=== DEBUG: Success! Title: {info.get('title', 'N/A')}")
                    logger.info(f"=== DEBUG: ID: {info.get('id', 'N/A')}")
                    return info
                except json.JSONDecodeError as e:
                    logger.error(f"=== DEBUG: JSON parse error: {e}")
                    logger.error(f"=== DEBUG: Raw output: {result.stdout[:500]}")
            else:
                logger.error(f"=== DEBUG: yt-dlp failed with code {result.returncode}")
                
                # Try Python API as fallback
                logger.info("=== DEBUG: Trying Python API fallback")
                opts = {
                    'cookiefile': cookie_path,
                    'quiet': False,
                    'no_warnings': False,
                    'verbose': True,
                    'socket_timeout': 30,
                    'retries': 5,
                    'extract_flat': False,
                    'no_check_certificate': True,
                    'logger': logger,
                    'add_header': [
                        'User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                        'Accept:text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                        'Accept-Language:en-US,en;q=0.9',
                    ],
                }
                
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    logger.info(f"=== DEBUG: Python API succeeded!")
                    return info
            
        except subprocess.TimeoutExpired:
            logger.error("=== DEBUG: Subprocess timed out")
            raise Exception("Request timed out - Instagram may be blocking")
        except Exception as e:
            logger.error(f"=== DEBUG: All attempts failed: {str(e)}")
            logger.error(f"=== DEBUG: Error type: {type(e).__name__}")
            raise
    
    def _sync_download(self, uid, url, media_type):
        """Download Instagram media with debug logging"""
        cookie_path = str(self.cookies[uid])
        logger.info(f"=== DOWNLOAD DEBUG: URL: {url}")
        logger.info(f"=== DOWNLOAD DEBUG: Media type: {media_type}")
        
        base_opts = {
            'cookiefile': cookie_path,
            'quiet': False,
            'no_warnings': False,
            'verbose': True,
            'socket_timeout': 120,
            'retries': 50,
            'fragment_retries': 50,
            'no_mtime': True,
            'no_check_certificate': True,
            'logger': logger,
            'add_header': [
                'User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                'Accept:text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                'Accept-Language:en-US,en;q=0.9',
            ],
            'extractor_args': {
                'instagram': [
                    'no_rate_limit',
                ]
            },
        }
        
        if 'video' in media_type or 'reel' in media_type or 'tv' in media_type:
            opts = {
                **base_opts,
                'format': 'best[ext=mp4]/best',
                'outtmpl': str(DOWNLOADS_DIR / '%(id)s_%(format_id)s.%(ext)s'),
                'merge_output_format': 'mp4',
            }
        elif 'profile_pic' in media_type:
            opts = {
                **base_opts,
                'playlist_items': '0',
                'skip_download': True,
                'writethumbnail': True,
                'outtmpl': str(DOWNLOADS_DIR / '%(id)s_profile.%(ext)s'),
            }
        else:
            opts = {
                **base_opts,
                'format': 'best',
                'outtmpl': str(DOWNLOADS_DIR / '%(id)s_%(format_id)s.%(ext)s'),
            }
        
        # Rate limiting delay
        import random
        delay = random.uniform(2, 5)
        logger.info(f"=== DOWNLOAD DEBUG: Waiting {delay:.1f}s before download")
        time.sleep(delay)
        
        try:
            logger.info(f"=== DOWNLOAD DEBUG: Starting yt-dlp extraction")
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if not info:
                    raise FileNotFoundError("yt-dlp returned no info")
                    
                title = info.get('title', 'Instagram Media')
                vid = info.get('id', '')
                logger.info(f"=== DOWNLOAD DEBUG: Title: {title}")
                logger.info(f"=== DOWNLOAD DEBUG: ID: {vid}")
                
                downloaded_files = []
                
                entries = info.get('entries') or [info]
                logger.info(f"=== DOWNLOAD DEBUG: Processing {len(entries)} entries")
                
                for i, entry in enumerate(entries):
                    if entry is None:
                        logger.warning(f"=== DOWNLOAD DEBUG: Entry {i} is None")
                        continue
                    
                    logger.info(f"=== DOWNLOAD DEBUG: Entry {i} keys: {list(entry.keys())[:10]}")
                    
                    for rd in entry.get('requested_downloads', []):
                        fp = rd.get('filepath')
                        if fp and Path(fp).exists():
                            logger.info(f"=== DOWNLOAD DEBUG: Found requested download: {fp}")
                            downloaded_files.append(fp)
                    
                    if not entry.get('requested_downloads'):
                        logger.info(f"=== DOWNLOAD DEBUG: No requested_downloads, scanning directory")
                        for ext in ('.jpg', '.jpeg', '.png', '.webp', '.mp4', '.webm', '.mp3'):
                            pattern = f'{vid}*{ext}'
                            for f in DOWNLOADS_DIR.glob(pattern):
                                fp = str(f)
                                if fp not in downloaded_files:
                                    logger.info(f"=== DOWNLOAD DEBUG: Found by pattern: {fp}")
                                    downloaded_files.append(fp)
                
                if not downloaded_files:
                    logger.error(f"=== DOWNLOAD DEBUG: No files found, listing recent files")
                    recent_files = sorted(
                        DOWNLOADS_DIR.iterdir(),
                        key=lambda x: x.stat().st_mtime,
                        reverse=True
                    )
                    for f in recent_files[:20]:
                        logger.info(f"  Recent file: {f.name} ({f.stat().st_mtime})")
                    raise FileNotFoundError(f"No files downloaded for {url}. ID: {vid}")
                
                logger.info(f"=== DOWNLOAD DEBUG: Success! {len(downloaded_files)} files")
                return downloaded_files, title, vid
                
        except Exception as e:
            logger.error(f"=== DOWNLOAD DEBUG: Failed: {str(e)}")
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
            "🔍 Use /debug to test cookies\n"
            f"🗑️ Files auto-deleted after {self.config.STORAGE_DAYS}d.",
            parse_mode=ParseMode.MARKDOWN, 
            reply_markup=self._menu(u.effective_user.id))
    
    async def help_cmd(self, u, c):
        await u.message.reply_text(
            "📚 Send any Instagram link to download.\n\n"
            "Commands:\n"
            "/cookies - Upload Instagram cookies\n"
            "/debug - Test cookie validity\n"
            "/recent - View recent downloads\n"
            "/start - Main menu",
            parse_mode=ParseMode.MARKDOWN, 
            reply_markup=self._menu(u.effective_user.id))
    
    async def debug_cmd(self, u, c):
        """Debug command to test cookie validity and Instagram connectivity"""
        uid = u.effective_user.id
        msg = u.message
        
        if uid not in self.cookies:
            await msg.reply_text("❌ No cookies uploaded. Use /cookies first.")
            return
        
        cookie_path = self._cookie_path(uid)
        status_msg = await msg.reply_text("🔍 Running diagnostics...")
        
        results = []
        
        # 1. Check file existence
        results.append(f"📁 Cookie file: {cookie_path}")
        if not cookie_path.exists():
            await status_msg.edit_text("❌ Cookie file not found!\n" + "\n".join(results))
            return
        
        # 2. Analyze cookie content
        content = cookie_path.read_text()
        lines = content.split('\n')
        results.append(f"📄 Lines: {len(lines)}")
        
        essential = ['sessionid', 'ds_user_id', 'csrftoken', 'mid', 'ig_did']
        found = []
        missing = []
        for cookie in essential:
            if cookie in content:
                found.append(cookie)
                idx = content.find(cookie)
                snippet = content[idx:idx+100].replace('\n', ' ')
                results.append(f"✅ {cookie}: ...{snippet[30:80]}...")
            else:
                missing.append(cookie)
                results.append(f"❌ {cookie}: MISSING")
        
        # 3. Check DNS
        try:
            ip = socket.gethostbyname('instagram.com')
            results.append(f"🌐 DNS: instagram.com → {ip}")
        except Exception as e:
            results.append(f"❌ DNS: {str(e)[:100]}")
        
        # 4. Test with yt-dlp
        results.append("🔄 Testing Instagram API...")
        await status_msg.edit_text("\n".join(results))
        
        try:
            cmd = [
                'yt-dlp',
                '--cookies', str(cookie_path),
                '--dump-json',
                '--no-download',
                '--verbose',
                '--no-check-certificate',
                'https://www.instagram.com/instagram/',
                '--playlist-items', '0'
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                results.append("✅ API: Connection successful!")
                try:
                    info = json.loads(result.stdout)
                    results.append(f"📱 Profile: {info.get('title', 'N/A')}")
                    results.append(f"🆔 ID: {info.get('id', 'N/A')}")
                except:
                    results.append("✅ API: Response received but couldn't parse")
            else:
                results.append(f"❌ API: Failed with code {result.returncode}")
                # Show first error line
                error_lines = [l for l in result.stderr.split('\n') if 'ERROR' in l or 'error' in l.lower()]
                for line in error_lines[:3]:
                    results.append(f"  ⚠️ {line[:150]}")
        
        except subprocess.TimeoutExpired:
            results.append("❌ API: Timed out after 30s")
        except Exception as e:
            results.append(f"❌ API: {str(e)[:200]}")
        
        # 5. Test with a specific post if one was recently attempted
        if uid in self._pending_urls:
            url, _, _, _ = self._pending_urls[uid]
            results.append(f"🔄 Testing last URL: {url[:50]}...")
            
            try:
                cmd = [
                    'yt-dlp',
                    '--cookies', str(cookie_path),
                    '--dump-json',
                    '--no-download',
                    '--verbose',
                    '--no-check-certificate',
                    url
                ]
                
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                
                if result.returncode == 0:
                    results.append("✅ Last URL: Worked with direct command!")
                    results.append("⚠️ Issue may be with Python API wrapper")
                else:
                    results.append(f"❌ Last URL: Failed with code {result.returncode}")
                    # Show specific Instagram errors
                    for line in result.stderr.split('\n'):
                        if 'instagram' in line.lower() or 'login' in line.lower() or 'private' in line.lower():
                            results.append(f"  ⚠️ {line[:200]}")
            
            except Exception as e:
                results.append(f"❌ Last URL test: {str(e)[:200]}")
        
        await status_msg.edit_text("\n".join(results))
    
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
        
        logger.info(f"=== USER {uid}: Received URL: {url}")
        
        if uid not in self.cookies:
            await u.message.reply_text(
                "❌ Upload Instagram cookies first!\n"
                "Use /cookies command.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🍪 Upload Cookies", callback_data='c')
                ]]))
            return
        
        # Validate cookies before attempting download
        if not self._validate_cookies(uid):
            logger.warning(f"=== USER {uid}: Cookies appear invalid")
            await u.message.reply_text(
                "⚠️ Cookies appear invalid or expired.\n"
                "Please re-export cookies from your browser.\n\n"
                "Make sure you're logged into Instagram and use:\n"
                "'Get cookies.txt LOCALLY' extension.\n\n"
                "Use /debug to diagnose the issue.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🍪 Upload New Cookies", callback_data='c'),
                    InlineKeyboardButton("🔍 Debug", callback_data='debug'),
                ]]))
            return
        
        logger.info(f"=== USER {uid}: Cookies validated OK")
        
        media_type, media_id = self._parse_instagram_url(url)
        logger.info(f"=== USER {uid}: Parsed as {media_type}/{media_id}")
        
        if not media_id:
            await u.message.reply_text("❌ Invalid Instagram URL.")
            return
        
        await self._show_format_choice(uid, url, media_type, media_id, u.message)
    
    async def _show_format_choice(self, uid, url, media_type, media_id, msg):
        s = await msg.reply_text("🔍 Fetching media info...")
        
        # Check network connectivity first
        try:
            socket.gethostbyname('instagram.com')
            logger.info(f"=== USER {uid}: DNS resolution OK")
        except Exception as e:
            logger.error(f"=== USER {uid}: DNS resolution failed: {e}")
            await s.edit_text(
                "❌ Cannot reach Instagram. Check network/VPN.\n"
                "Instagram may be blocked in your region.",
                reply_markup=self._menu(uid))
            return
        
        try:
            # Run in executor with timeout
            info = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None, self._sync_fetch_info, uid, url
                ),
                timeout=45
            )
            
            title = info.get('title', 'Instagram Media')
            logger.info(f"=== USER {uid}: Successfully fetched: {title}")
            
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
                
        except asyncio.TimeoutError:
            logger.error(f"=== USER {uid}: Request timed out")
            await s.edit_text(
                "❌ Request timed out.\n\n"
                "Possible issues:\n"
                "• Instagram is rate limiting you\n"
                "• Cookie session expired\n"
                "• Network is too slow\n\n"
                "Try again in a few minutes or use /debug",
                reply_markup=self._menu(uid))
        except Exception as e:
            error_msg = str(e)
            logger.error(f"=== USER {uid}: Final error: {error_msg}")
            
            # Check debug log for details
            logger.error(f"=== Check debug.log for full traceback")
            
            if "login" in error_msg.lower() or "cookie" in error_msg.lower():
                user_msg = (
                    "❌ Authentication failed.\n\n"
                    "Your cookies may be expired. Please:\n"
                    "1. Login to Instagram in browser\n"
                    "2. Re-export cookies.txt\n"
                    "3. Upload again with /cookies\n"
                    "4. Test with /debug"
                )
            elif "private" in error_msg.lower():
                user_msg = (
                    "❌ This content is private.\n\n"
                    "You need to follow this account with the\n"
                    "same account used for cookies."
                )
            elif "rate" in error_msg.lower() or "429" in error_msg:
                user_msg = (
                    "❌ Instagram rate limit reached.\n\n"
                    "Wait a few minutes before trying again."
                )
            else:
                user_msg = (
                    f"❌ Failed to fetch media info.\n\n"
                    f"Error: {error_msg[:200]}\n\n"
                    "Use /debug to diagnose the issue.\n"
                    "Check debug.log on server for details."
                )
            
            await s.edit_text(user_msg, reply_markup=self._menu(uid))
    
    async def _choose_format(self, u, c):
        q = u.callback_query
        await q.answer()
        uid, fmt = u.effective_user.id, q.data
        
        logger.info(f"=== USER {uid}: Chose format {fmt}")
        
        if uid not in self._pending_urls:
            await q.message.reply_text("Session expired. Send the link again.")
            return
        
        url, media_id, media_type, title = self._pending_urls[uid]
        
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
        
        # Check for existing download and resend if found
        existing = self._find_existing(uid, media_id, download_type)
        if existing and existing.telegram_file_ids:
            await q.answer("Resending...")
            await self._resend_media(q.message, existing)
            return
        elif existing:
            await q.answer("Already downloaded! Uploading...")
            await self._upload_media(q.message, existing)
            return
        
        # Start background download
        await q.message.edit_text(f"⏳ Downloading {download_type}...")
        task = asyncio.create_task(self._download_and_upload(uid, url, q.message, download_type, title))
        self._download_tasks[uid] = task
    
    async def _download_and_upload(self, uid, url, msg, download_type, title):
        """Download media and upload directly to Telegram"""
        logger.info(f"=== USER {uid}: Starting download task for {download_type}")
        try:
            file_paths, dl_title, media_id = await asyncio.get_event_loop().run_in_executor(
                None, self._sync_download, uid, url, download_type)
            
            logger.info(f"=== USER {uid}: Downloaded {len(file_paths)} files")
            
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
            while len(self.media[uid]) > 50:
                old = self.media[uid].pop()
                for fp in old.file_paths:
                    Path(fp).unlink(missing_ok=True)
            self._save()
            
            # Upload to Telegram immediately
            await msg.edit_text(f"📤 Uploading to Telegram...")
            await self._upload_media(msg, record)
            
        except Exception as e:
            logger.error(f"=== USER {uid}: Download error: {str(e)[:200]}")
            await msg.edit_text(
                f"❌ Download failed: {str(e)[:200]}\n\n"
                "Possible reasons:\n"
                "• Private account (must follow)\n"
                "• Story expired (24h limit)\n"
                "• Instagram rate limit\n"
                "• Invalid cookies\n\n"
                "Use /debug to diagnose.",
                reply_markup=self._menu(uid))
        finally:
            self._download_tasks.pop(uid, None)
    
    async def _upload_media(self, msg, record):
        """Upload media files to Telegram"""
        existing_files = [fp for fp in record.file_paths if Path(fp).exists()]
        if not existing_files:
            await msg.edit_text("❌ Files deleted.", reply_markup=self._menu(msg.chat.id))
            return
        
        total_mb = sum(Path(fp).stat().st_size for fp in existing_files) / 1024 / 1024
        if total_mb > self.config.MAX_TELEGRAM_FILE_SIZE:
            await msg.edit_text(
                f"⚠️ File too large ({total_mb:.1f}MB). Max: {self.config.MAX_TELEGRAM_FILE_SIZE}MB",
                reply_markup=self._menu(msg.chat.id))
            return
        
        try:
            sent_ids = []
            for i, fp in enumerate(existing_files):
                ext = Path(fp).suffix.lower()
                is_video = ext in ('.mp4', '.webm', '.mkv')
                is_image = ext in ('.jpg', '.jpeg', '.png', '.webp')
                
                file_count = len(existing_files)
                caption = f"{record.title[:200]}"
                if file_count > 1:
                    caption += f" ({i+1}/{file_count})"
                
                with open(fp, 'rb') as f:
                    if is_video:
                        sent = await msg.reply_video(
                            video=f, 
                            caption=caption,
                            supports_streaming=True)
                        sent_ids.append(sent.video.file_id)
                    elif is_image:
                        sent = await msg.reply_photo(
                            photo=f,
                            caption=caption)
                        sent_ids.append(sent.photo[-1].file_id)
                    else:
                        sent = await msg.reply_document(
                            document=f,
                            caption=caption)
                        sent_ids.append(sent.document.file_id)
                
                if file_count > 1:
                    await asyncio.sleep(0.5)
            
            record.telegram_file_ids = sent_ids
            self._save()
            await msg.delete()
            
            # Cleanup files after successful upload
            for fp in record.file_paths:
                Path(fp).unlink(missing_ok=True)
                
        except Exception as e:
            logger.error(f"Upload error: {str(e)[:100]}")
            await msg.edit_text(
                "❌ Upload failed. Try again.",
                reply_markup=self._menu(msg.chat.id))
    
    async def _resend_media(self, msg, record):
        """Resend media using cached file IDs"""
        if not record.telegram_file_ids:
            await self._upload_media(msg, record)
            return
        
        try:
            for file_id in record.telegram_file_ids:
                try:
                    await msg.reply_video(video=file_id, caption=record.title[:200], supports_streaming=True)
                except:
                    try:
                        await msg.reply_photo(photo=file_id, caption=record.title[:200])
                    except:
                        await msg.reply_document(document=file_id, caption=record.title[:200])
                await asyncio.sleep(0.3)
            await msg.delete()
        except Exception as e:
            logger.error(f"Resend error: {str(e)[:100]}")
            await self._upload_media(msg, record)
    
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
            ex = "✅" if m.telegram_file_ids else "💾"
            em = type_emoji.get(m.media_type, '📱')
            file_count = len(m.file_paths)
            count_str = f" ({file_count} files)" if file_count > 1 else ""
            txt += f"{ex} {em} *{i}.* {self._esc(m.title[:50])}\n"
            txt += f"   📦 {m.total_size / 1024 / 1024:.2f}MB{count_str} | {m.download_time}\n\n"
        
        kb = []
        for i, m in enumerate(pv, page * pp + 1):
            em = type_emoji.get(m.media_type, '📱')
            if m.telegram_file_ids:
                kb.append([InlineKeyboardButton(
                    f"📤 Resend {em} {i}. {m.title[:35]}",
                    callback_data=f'resend_{page * pp + (i - page * pp - 1)}')])
        
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f'p_{page - 1}'))
        if page < tp - 1:
            nav.append(InlineKeyboardButton("➡️", callback_data=f'p_{page + 1}'))
        if nav:
            kb.append(nav)
        kb.append([InlineKeyboardButton("🗑️ Clear History", callback_data='clear_hist'),
                   InlineKeyboardButton("🔙 Menu", callback_data='b')])
        
        await msg.reply_text(
            txt, 
            parse_mode=ParseMode.MARKDOWN, 
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(kb))
    
    async def _resend_from_history(self, u, c):
        q = u.callback_query
        await q.answer("Resending...")
        uid, idx = u.effective_user.id, int(q.data.split('_')[1])
        media_list = self.media.get(uid, [])
        if 0 <= idx < len(media_list):
            await self._resend_media(q.message, media_list[idx])
            await q.message.delete()
    
    async def _clear_history(self, u, c):
        q = u.callback_query
        await q.answer()
        uid = u.effective_user.id
        if uid in self.media:
            del self.media[uid]
            self._save()
        await q.message.edit_text(
            "🗑️ History cleared.",
            reply_markup=InlineKeyboardMarkup([[
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
            
            # Validate immediately
            if self._validate_cookies(uid):
                await u.message.reply_text(
                    "✅ Cookies saved and validated!\n\n"
                    "Now send any Instagram link to download.\n\n"
                    "Supported:\n"
                    "• Posts (images & videos)\n"
                    "• Reels\n"
                    "• Stories\n"
                    "• IGTV\n"
                    "• Profile Pictures",
                    reply_markup=self._menu(uid))
            else:
                await u.message.reply_text(
                    "⚠️ Cookies saved but may be invalid.\n"
                    "Missing essential cookies (sessionid, ds_user_id).\n\n"
                    "Make sure you:\n"
                    "1. Are logged into Instagram in browser\n"
                    "2. Use 'Get cookies.txt LOCALLY' extension\n"
                    "3. Export all cookies, not just selected ones\n\n"
                    "Use /debug to check cookie status.",
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
            'b': lambda: q.message.edit_text("📋 Main Menu:", reply_markup=self._menu(uid)),
            'r': lambda: self._show_recent(u, c),
            'c': lambda: self._ask_cookies(u, c),
            'debug': lambda: self.debug_cmd(u, c),
            'cs': lambda: q.message.edit_text(
                "✅ Cookies ready!" if uid in self.cookies else "❌ Use /cookies to upload.",
                reply_markup=self._menu(uid)),
            'vc': lambda: q.message.edit_text(
                f"📦 {len(self.media.get(uid, []))} downloaded items",
                reply_markup=self._menu(uid)),
            'clear_hist': lambda: self._clear_history(u, c),
        }
        
        if d in routes:
            await routes[d]()
        elif d.startswith('fmt_'):
            await self._choose_format(u, c)
        elif d.startswith('resend_'):
            await self._resend_from_history(u, c)
        elif d.startswith('p_'):
            await self._show_recent(u, c, int(d.split('_')[1]))
    
    def run(self):
        app = Application.builder().token(self.config.BOT_TOKEN).build()
        
        app.add_handler(CommandHandler('start', self.start_cmd))
        app.add_handler(CommandHandler('help', self.help_cmd))
        app.add_handler(CommandHandler('recent', self.recent_cmd))
        app.add_handler(CommandHandler('debug', self.debug_cmd))
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
        
        logger.info("Instagram Bot starting with DEBUG logging...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    InstagramDownloaderBot().run()