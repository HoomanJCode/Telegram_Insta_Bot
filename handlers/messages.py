# handlers/messages.py
import asyncio
import logging
from pathlib import Path
import shutil
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from utils.helpers import extract_url, check_whitelist
from utils.telegram_sender import resend_by_file_ids, send_media_batch
from core.downloader import download_media

logger = logging.getLogger(__name__)

async def on_private_msg(bot_instance, u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    
    url = extract_url(u.message.text)
    if not url:
        return
    
    # Check cache first - works for everyone
    cached = bot_instance.file_id_cache.get(url)
    if cached and cached.get('file_ids'):
        await resend_by_file_ids(u.message.chat_id, c, cached)
        return
    
    # Need cookies for new downloads
    if uid not in bot_instance.cookies:
        loaded = await bot_instance._ensure_cookies_loaded(uid, c)
        if not loaded:
            # Only show cookie prompt to whitelisted users
            if check_whitelist(uid, bot_instance.config):
                await u.message.reply_text(
                    "❌ Cookies not available.\n"
                    "Use /cookies to upload your Instagram cookies.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🍪 Upload Cookies", callback_data='c')
                    ]]))
            return
    
    await _auto_download_and_send(bot_instance, uid, url, u.message.chat_id, c)

async def on_group_msg(bot_instance, u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    chat_id = u.effective_chat.id
    
    if not await bot_instance._is_group_allowed(chat_id, c):
        return
    
    url = extract_url(u.message.text)
    if not url:
        return
    
    # Check cache first
    cached = bot_instance.file_id_cache.get(url)
    if cached and cached.get('file_ids'):
        await resend_by_file_ids(chat_id, c, cached, reply_to_message_id=u.message.message_id)
        return
    
    # Need cookies for new downloads
    if uid not in bot_instance.cookies:
        loaded = await bot_instance._ensure_cookies_loaded(uid, c)
        if not loaded:
            return  # Silent in groups
    
    status = await u.message.reply_text("⏳ Downloading...")
    try:
        await _auto_download_and_send(bot_instance, uid, url, chat_id, c, reply_to_message_id=u.message.message_id)
    finally:
        await status.delete()

async def _auto_download_and_send(bot_instance, uid, url, chat_id, context, reply_to_message_id: int = None):
    cached = bot_instance.file_id_cache.get(url)
    if cached and cached.get('file_ids'):
        success = await resend_by_file_ids(chat_id, context, cached, reply_to_message_id)
        if success:
            return
    
    lock = bot_instance._get_download_lock(url)
    
    async with lock:
        cached = bot_instance.file_id_cache.get(url)
        if cached and cached.get('file_ids'):
            await resend_by_file_ids(chat_id, context, cached, reply_to_message_id)
            return
        
        try:
            file_paths, title, username = await asyncio.get_event_loop().run_in_executor(
                None, download_media, uid, url, bot_instance.cookies[uid])
            
            file_count = len(file_paths)
            
            caption = title
            if username:
                caption = f"📱 @{username}\n{title}"
            if file_count > 1:
                caption += f"\n\n📸 {file_count} images"
            
            file_ids = await send_media_batch(chat_id, context, file_paths, caption, reply_to_message_id)
            
            if file_ids:
                bot_instance.file_id_cache.add(url, file_ids, title, username)
            
            logger.info(f"Sent {len(file_ids)} files for {url[:80]}")
            
            for fp in file_paths:
                Path(fp).unlink(missing_ok=True)
            parent = Path(file_paths[0]).parent
            if parent.exists():
                shutil.rmtree(parent, ignore_errors=True)
            
        except Exception as e:
            logger.error(f"Download error: {str(e)[:200]}")
            if reply_to_message_id is None:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Failed: {str(e)[:200]}\n\n⚠️ Re-upload cookies with /cookies if needed")