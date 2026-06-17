#!/usr/bin/env python3
"""
Instagram Downloader Telegram Bot
Uses gallery-dl for reliable Instagram downloads
Auto-downloads and sends media, caches Telegram file IDs to prevent re-uploads
Supports inline mode and group chats
"""

import asyncio
import logging
import tempfile
import os
from pathlib import Path
from typing import Dict

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, InlineQueryHandler
)
from telegram.constants import ParseMode

from config import Config
from core.downloader import check_gallery_dl
from core.cache import FileIDCache
from core.cookies import load_cookie_ids, save_cookie_ids

from handlers.start import start_cmd
from handlers.messages import on_private_msg, on_group_msg
from handlers.inline import inline_query
from handlers.cookies_handler import ask_cookies, recv_cookies, WAITING_FOR_COOKIES
from handlers.callbacks import router

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
# Bot
# ---------------------------------------------------------------------------
class InstagramDownloaderBot:
    def __init__(self):
        self.config = Config()
        
        for d in [Path('data'), Path('downloads')]:
            d.mkdir(parents=True, exist_ok=True)
        
        self.cookies: Dict[int, str] = {}
        self.cookie_file_ids: Dict[int, str] = load_cookie_ids()
        self._pending_downloads: Dict[str, dict] = {}
        self._allowed_groups: Dict[int, bool] = {}
        self._download_locks: Dict[str, asyncio.Lock] = {}
        self._cookie_locks: Dict[int, asyncio.Lock] = {}
        
        self.file_id_cache = FileIDCache(self.config.STORAGE_DAYS)
        
        check_gallery_dl()
    
    def _menu(self, uid):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        label = "🍪 Update Cookies" if uid in self.cookies or uid in self.cookie_file_ids else "🍪 Upload Cookies"
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(label, callback_data='c')],
        ])
    
    def _admin_menu(self, uid):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from utils.helpers import check_admin
        cache_count = len(self.file_id_cache)
        cookie_count = len(self.cookie_file_ids)
        label = "🍪 Update Cookies" if uid in self.cookies or uid in self.cookie_file_ids else "🍪 Upload Cookies"
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(label, callback_data='c')],
            [InlineKeyboardButton(f"📊 Stats: {cache_count} cached, {cookie_count} users", callback_data='admin_stats')],
        ])
    
    def _get_download_lock(self, url: str) -> asyncio.Lock:
        if url not in self._download_locks:
            self._download_locks[url] = asyncio.Lock()
        return self._download_locks[url]
    
    def _get_cookie_lock(self, uid: int) -> asyncio.Lock:
        if uid not in self._cookie_locks:
            self._cookie_locks[uid] = asyncio.Lock()
        return self._cookie_locks[uid]
    
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
    
    async def _is_group_allowed(self, chat_id, context) -> bool:
        from utils.helpers import check_whitelist
        whitelist = self.config.get_whitelist()
        if not whitelist:
            return True
        
        if chat_id in self._allowed_groups:
            return self._allowed_groups[chat_id]
        
        try:
            admins = await context.bot.get_chat_administrators(chat_id)
            for admin in admins:
                if admin.user.id in whitelist:
                    self._allowed_groups[chat_id] = True
                    return True
            self._allowed_groups[chat_id] = False
            return False
        except Exception as e:
            logger.error(f"Failed to check group admins for {chat_id}: {e}")
            return False
    
    async def _ask_cookies(self, u, c):
        return await ask_cookies(self, u, c)
    
    def run(self):
        app = Application.builder().token(self.config.BOT_TOKEN).build()
        
        app.add_handler(CommandHandler('start', lambda u, c: start_cmd(self, u, c)))
        app.add_handler(CommandHandler('help', lambda u, c: self._help_cmd(u, c)))
        app.add_handler(ConversationHandler(
            entry_points=[
                CommandHandler('cookies', lambda u, c: ask_cookies(self, u, c)),
                CallbackQueryHandler(lambda u, c: ask_cookies(self, u, c), pattern='^c$')
            ],
            states={
                WAITING_FOR_COOKIES: [
                    MessageHandler(filters.Document.FileExtension("txt"), lambda u, c: recv_cookies(self, u, c)),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: ask_cookies(self, u, c))
                ]
            },
            fallbacks=[
                CommandHandler('cancel', self._cancel_cmd),
                CallbackQueryHandler(lambda u, c: router(self, u, c), pattern='^b$')
            ],
            per_message=False))
        app.add_handler(CallbackQueryHandler(lambda u, c: router(self, u, c)))
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
            lambda u, c: on_group_msg(self, u, c)))
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            lambda u, c: on_private_msg(self, u, c)))
        app.add_handler(InlineQueryHandler(lambda u, c: inline_query(self, u, c)))
        
        logger.info(f"Instagram Bot starting (inline, groups, cookie refs: {len(self.cookie_file_ids)}, cache: {len(self.file_id_cache)})...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    
    async def _help_cmd(self, u, c):
        await u.message.reply_text(
            "📚 Just send an Instagram link to download!\n\n"
            "Commands:\n"
            "/cookies - Upload/Update Instagram cookies\n"
            "/start - Main menu\n\n"
            f"🌐 *Inline Mode:* Type @{c.bot.username} <link> in any chat\n"
            "👥 *Groups:* Send link in group, bot replies with media\n"
            "(requires you have cookies set up)",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self._menu(u.effective_user.id))
    
    async def _cancel_cmd(self, u, c):
        await u.message.reply_text("❌ Cancelled.", reply_markup=self._menu(u.effective_user.id))
        return ConversationHandler.END

if __name__ == '__main__':
    InstagramDownloaderBot().run()