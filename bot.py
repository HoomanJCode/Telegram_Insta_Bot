#!/usr/bin/env python3
"""
Instagram Downloader Telegram Bot
Uses gallery-dl for reliable Instagram downloads
Supports posts, reels, stories, and profile pictures
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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
COOKIES_DIR = DATA_DIR / 'cookies'
DOWNLOADS_DIR = Path('downloads')
WAITING_FOR_COOKIES = 1

INSTAGRAM_RE = re.compile(
    r'(https?://)?(www\.)?instagram\.com/('
    r'p/[^/?#\s]+|'
    r'reel/[^/?#\s]+|'
    r'stories/[^/?#\s]+|'
    r'[^/?#\s]+/?$'
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
        
        self._check_gallery_dl()
        self._load()
        self._start_cleanup()
    
    def _check_gallery_dl(self):
        try:
            result = subprocess.run(['gallery-dl', '--version'], capture_output=True, text=True)
            logger.info(f"gallery-dl version: {result.stdout.strip()}")
        except FileNotFoundError:
            logger.error("gallery-dl not found! Installing...")
            subprocess.run([sys.executable, '-m', 'pip', 'install', 'gallery-dl'], check=True)
    
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
        elif '/stories/' in url:
            m = re.search(r'/stories/([^/?#]+)', url)
            return ('story', m.group(1) if m else 'unknown')
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
        existing = self._get_existing_types(uid, media_id)
        kb = []
        
        if media_type == 'post':
            label = "🖼️ Post Images"
            if 'post' in existing:
                label = "✅ 🖼️ Post Images"
            kb.append([InlineKeyboardButton(label, callback_data='fmt_post')])
        
        elif media_type == 'reel':
            label = "🎬 Reel Video"
            if 'reel' in existing:
                label = "✅ 🎬 Reel Video"
            kb.append([InlineKeyboardButton(label, callback_data='fmt_reel')])
        
        elif media_type == 'story':
            label = "📖 Story"
            if 'story' in existing:
                label = "✅ 📖 Story"
            kb.append([InlineKeyboardButton(label, callback_data='fmt_story')])
        
        elif media_type == 'profile':
            label = "🖼️ Profile Picture"
            if 'profile' in existing:
                label = "✅ 🖼️ Profile Picture"
            kb.append([InlineKeyboardButton(label, callback_data='fmt_profile')])
        
        kb.append([InlineKeyboardButton("🔙 Cancel", callback_data='b')])
        return InlineKeyboardMarkup(kb)
    
    # --- gallery-dl helpers ---
    def _sync_fetch_info(self, uid, url):
        """Fetch media info using gallery-dl"""
        cookie_path = str(self.cookies[uid])
        
        try:
            cmd = ['gallery-dl', '--cookies', cookie_path, '--dump-json', url]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            if result.returncode != 0:
                raise Exception(f"gallery-dl failed: {result.stderr[:200]}")
            
            data = json.loads(result.stdout)
            
            # gallery-dl returns a list: first element is [2, {...post_info...}]
            # subsequent elements are [3, url, {...image_info...}]
            info = {}
            if isinstance(data, list) and len(data) > 0:
                first = data[0]
                if isinstance(first, list) and len(first) >= 2:
                    meta = first[1]
                    if isinstance(meta, dict):
                        info = {
                            'title': meta.get('description', '') or f"Post by {meta.get('username', 'Unknown')}",
                            'id': meta.get('post_shortcode', meta.get('post_id', '')),
                            'username': meta.get('username', ''),
                            'fullname': meta.get('fullname', ''),
                            'count': meta.get('count', len(data) - 1),
                        }
            
            if not info:
                raise Exception("Could not parse media info from gallery-dl output")
            
            logger.info(f"Fetched: {info['title'][:100]} by {info['username']}")
            return info
            
        except subprocess.TimeoutExpired:
            raise Exception("Request timed out")
        except Exception as e:
            logger.error(f"Info fetch error: {e}")
            raise
    
    def _sync_download(self, uid, url, media_type):
        """Download Instagram media using gallery-dl"""
        cookie_path = str(self.cookies[uid])
        
        download_id = f"{uid}_{int(time.time())}"
        output_dir = DOWNLOADS_DIR / download_id
        output_dir.mkdir(parents=True, exist_ok=True)
        
        try:
            cmd = ['gallery-dl', '--cookies', cookie_path, '--dest', str(output_dir), url]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            
            if result.returncode != 0:
                raise Exception(f"Download failed: {result.stderr[:200]}")
            
            all_files = []
            for f in output_dir.rglob('*'):
                if f.is_file():
                    all_files.append(str(f))
            
            if not all_files:
                raise Exception("No files downloaded")
            
            logger.info(f"Downloaded {len(all_files)} files")
            
            # Get info
            info = self._sync_fetch_info(uid, url)
            title = info.get('title', 'Instagram Media')
            media_id = info.get('id', url.split('/')[-2] if '/p/' in url else 'unknown')
            
            return all_files, title, media_id
            
        except Exception as e:
            shutil.rmtree(output_dir, ignore_errors=True)
            raise
    
    # --- Async handlers ---
    async def start_cmd(self, u, c):
        if not self._ok(u.effective_user.id):
            return
        await u.message.reply_text(
            f"👋 Welcome {u.effective_user.first_name}!\n\n"
            "📱 *Instagram Downloader Bot*\n\n"
            "💡 Supported content:\n"
            "• Posts (images)\n"
            "• Reels\n"
            "• Stories\n"
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
            info = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None, self._sync_fetch_info, uid, url
                ),
                timeout=30
            )
            
            title = info.get('title', 'Instagram Media')
            self._pending_urls[uid] = (url, media_id, media_type, title)
            
            existing = self._get_existing_types(uid, media_id)
            dl = ""
            if existing:
                dl = "\n✅ Already downloaded"
            
            type_labels = {
                'post': '📷 Post', 'reel': '🎬 Reel',
                'story': '📖 Story', 'profile': '👤 Profile'
            }
            
            await s.edit_text(
                f"{type_labels.get(media_type, '📱')} - *{self._esc(title[:200])}*{dl}\n\n"
                f"Choose format to download:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self._format_choice_keyboard(uid, media_type, media_id))
                
        except asyncio.TimeoutError:
            await s.edit_text("❌ Request timed out.", reply_markup=self._menu(uid))
        except Exception as e:
            await s.edit_text(f"❌ Failed: {str(e)[:200]}", reply_markup=self._menu(uid))
    
    async def _choose_format(self, u, c):
        q = u.callback_query
        await q.answer()
        uid, fmt = u.effective_user.id, q.data
        
        if uid not in self._pending_urls:
            await q.message.reply_text("Session expired. Send the link again.")
            return
        
        url, media_id, media_type, title = self._pending_urls[uid]
        
        format_map = {
            'fmt_post': 'post',
            'fmt_reel': 'reel',
            'fmt_story': 'story',
            'fmt_profile': 'profile',
        }
        
        download_type = format_map.get(fmt)
        if not download_type:
            return
        
        existing = self._find_existing(uid, media_id, download_type)
        if existing and existing.telegram_file_ids:
            await q.answer("Resending...")
            await self._resend_media(q.message, existing)
            return
        elif existing:
            await q.answer("Already downloaded! Uploading...")
            await self._upload_media(q.message, existing)
            return
        
        await q.message.edit_text(f"⏳ Downloading...")
        task = asyncio.create_task(self._download_and_upload(uid, url, q.message, download_type, title))
        self._download_tasks[uid] = task
    
    async def _download_and_upload(self, uid, url, msg, download_type, title):
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
            while len(self.media[uid]) > 50:
                old = self.media[uid].pop()
                for fp in old.file_paths:
                    Path(fp).unlink(missing_ok=True)
            self._save()
            
            await msg.edit_text("📤 Uploading to Telegram...")
            await self._upload_media(msg, record)
            
        except Exception as e:
            logger.error(f"Download error: {str(e)[:200]}")
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
    
    async def _upload_media(self, msg, record):
        existing_files = [fp for fp in record.file_paths if Path(fp).exists()]
        if not existing_files:
            await msg.edit_text("❌ Files deleted.", reply_markup=self._menu(msg.chat.id))
            return
        
        total_mb = sum(Path(fp).stat().st_size for fp in existing_files) / 1024 / 1024
        if total_mb > self.config.MAX_TELEGRAM_FILE_SIZE:
            await msg.edit_text(
                f"⚠️ Too large ({total_mb:.1f}MB). Max: {self.config.MAX_TELEGRAM_FILE_SIZE}MB",
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
                        sent = await msg.reply_video(video=f, caption=caption, supports_streaming=True)
                        sent_ids.append(sent.video.file_id)
                    elif is_image:
                        sent = await msg.reply_photo(photo=f, caption=caption)
                        sent_ids.append(sent.photo[-1].file_id)
                    else:
                        sent = await msg.reply_document(document=f, caption=caption)
                        sent_ids.append(sent.document.file_id)
                
                if file_count > 1:
                    await asyncio.sleep(0.5)
            
            record.telegram_file_ids = sent_ids
            self._save()
            await msg.delete()
            
            # Clean up files
            for fp in record.file_paths:
                Path(fp).unlink(missing_ok=True)
            # Remove empty dirs
            for fp in record.file_paths:
                parent = Path(fp).parent
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
                
        except Exception as e:
            logger.error(f"Upload error: {str(e)[:100]}")
            await msg.edit_text("❌ Upload failed.", reply_markup=self._menu(msg.chat.id))
    
    async def _resend_media(self, msg, record):
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
            await msg.reply_text("📭 No recent downloads.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Menu", callback_data='b')
                ]]))
            return
        
        pp, tp = 5, max(1, (len(media_list) + 4) // 5)
        page = max(0, min(page, tp - 1))
        pv = media_list[page * pp:(page + 1) * pp]
        
        type_emoji = {'post': '🖼️', 'reel': '🎬', 'story': '📖', 'profile': '🖼️'}
        
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
        kb.append([InlineKeyboardButton("🗑️ Clear", callback_data='clear_hist'),
                   InlineKeyboardButton("🔙 Menu", callback_data='b')])
        
        await msg.reply_text(txt, parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True, reply_markup=InlineKeyboardMarkup(kb))
    
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
        await q.message.edit_text("🗑️ History cleared.",
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
            self._save()
            await u.message.reply_text(
                "✅ Cookies saved!\n\nSend any Instagram link to download.",
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
            'r': lambda: self._show_recent(u, c),
            'c': lambda: self._ask_cookies(u, c),
            'cs': lambda: q.message.edit_text(
                "✅ Ready!" if uid in self.cookies else "❌ Use /cookies",
                reply_markup=self._menu(uid)),
            'vc': lambda: q.message.edit_text(
                f"📦 {len(self.media.get(uid, []))} files",
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
        
        logger.info("Instagram Bot starting with gallery-dl...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    InstagramDownloaderBot().run()