import asyncio
import logging
import time
from uuid import uuid4
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from utils.helpers import extract_url
from core.downloader import download_media

logger = logging.getLogger(__name__)

async def inline_query(bot_instance, update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip()
    bot_username = context.bot.username
    
    url = extract_url(query)
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
    
    cached = bot_instance.file_id_cache.get(url)
    
    if cached:
        file_ids = cached.get('file_ids', [])
        title = cached.get('title', 'Instagram Media')
        
        download_id = str(uuid4())[:8]
        bot_instance._pending_downloads[download_id] = {
            'uid': 0,
            'url': url,
            'status': 'cached',
            'started_at': time.time(),
        }
        
        bot_link = f"https://t.me/{bot_username}?start=dl_{download_id}"
        
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title=f"📱 {title[:50]}",
                description=f"✅ Ready ({len(file_ids)} files) - Tap to get in bot",
                input_message_content=InputTextMessageContent(
                    f"📱 [Get Instagram Media]({bot_link})",
                    parse_mode=ParseMode.MARKDOWN
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📥 Get Media", url=bot_link)
                ]])
            )
        ]
        await update.inline_query.answer(results, cache_time=30)
        return
    
    if not bot_instance.cookies and not bot_instance.cookie_file_ids:
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="❌ No cookies configured",
                description="An admin needs to set up cookies first",
                input_message_content=InputTextMessageContent(
                    "❌ Bot not configured. Contact admin to set up Instagram cookies."
                )
            )
        ]
        await update.inline_query.answer(results, cache_time=10)
        return
    
    download_id = str(uuid4())[:8]
    cookie_uid = next((u for u in bot_instance.cookies if bot_instance.cookies[u]), None)
    if not cookie_uid:
        cookie_uid = next((u for u in bot_instance.cookie_file_ids if bot_instance.cookie_file_ids[u]), None)
    
    if cookie_uid:
        bot_instance._pending_downloads[download_id] = {
            'uid': cookie_uid,
            'url': url,
            'status': 'pending',
            'started_at': time.time(),
        }
        
        asyncio.create_task(_background_download(bot_instance, download_id, cookie_uid, url))
        
        bot_link = f"https://t.me/{bot_username}?start=dl_{download_id}"
        
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="📥 Download started...",
                description="Tap to check status in bot",
                input_message_content=InputTextMessageContent(
                    f"⏳ [Downloading...]({bot_link})",
                    parse_mode=ParseMode.MARKDOWN
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📥 Check Download", url=bot_link)
                ]])
            )
        ]
        await update.inline_query.answer(results, cache_time=10)
    else:
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="❌ No cookies available",
                description="Set up cookies in bot first",
                input_message_content=InputTextMessageContent(
                    f"❌ Set up Instagram cookies first: https://t.me/{bot_username}?start=cookies"
                )
            )
        ]
        await update.inline_query.answer(results, cache_time=10)

async def _background_download(bot_instance, download_id: str, uid: int, url: str):
    try:
        bot_instance._pending_downloads[download_id]['status'] = 'downloading'
        
        if uid not in bot_instance.cookies:
            bot_instance._pending_downloads[download_id]['status'] = 'failed'
            bot_instance._pending_downloads[download_id]['error'] = 'Cookies not loaded'
            return
        
        file_paths, title, username = await asyncio.get_event_loop().run_in_executor(
            None, download_media, uid, url, bot_instance.cookies[uid])
        
        if not file_paths:
            bot_instance._pending_downloads[download_id]['status'] = 'failed'
            bot_instance._pending_downloads[download_id]['error'] = 'No media found'
            return
        
        total_size = sum(Path(file_paths[0]).stat().st_size for fp in file_paths)  # Fixed
        
        bot_instance._pending_downloads[download_id]['status'] = 'ready'
        bot_instance._pending_downloads[download_id]['file_paths'] = file_paths
        bot_instance._pending_downloads[download_id]['title'] = title
        bot_instance._pending_downloads[download_id]['username'] = username
        bot_instance._pending_downloads[download_id]['url'] = url
        
    except Exception as e:
        logger.error(f"Background download failed: {e}")
        bot_instance._pending_downloads[download_id]['status'] = 'failed'
        bot_instance._pending_downloads[download_id]['error'] = str(e)[:200]