import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from utils.helpers import check_whitelist, check_admin, get_cookie_status_text
from utils.telegram_sender import resend_by_file_ids, send_media_batch
from core.downloader import download_media
from core.cache import FileIDCache
from pathlib import Path
import shutil

logger = logging.getLogger(__name__)

async def start_cmd(bot_instance, u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    args = c.args
    
    logger.info(f"Start command from {uid}, args: {args}")
    
    if args:
        deep_link = args[0]
        logger.info(f"Deep link: {deep_link}")
        
        if deep_link.startswith('dl_'):
            download_ref = deep_link[3:]
            logger.info(f"Looking for download ref: {download_ref}")
            
            if download_ref in bot_instance._pending_downloads:
                pending = bot_instance._pending_downloads[download_ref]
                status = pending.get('status', 'unknown')
                logger.info(f"Found pending download: {status}")
                
                if status == 'cached':
                    url = pending.get('url', '')
                    cached = bot_instance.file_id_cache.get(url)
                    if cached:
                        file_ids = cached.get('file_ids', [])
                        title = cached.get('title', 'Instagram Media')
                        status_msg = await u.message.reply_text(f"📤 Sending {len(file_ids)} files...")
                        
                        for i, fid in enumerate(file_ids):
                            caption = title if i == 0 else None
                            try:
                                await c.bot.send_video(chat_id=uid, video=fid, caption=caption, supports_streaming=True)
                            except:
                                try:
                                    await c.bot.send_photo(chat_id=uid, photo=fid, caption=caption)
                                except:
                                    await c.bot.send_document(chat_id=uid, document=fid, caption=caption)
                            await asyncio.sleep(0.3)
                        await status_msg.delete()
                        return
                
                elif status == 'ready':
                    file_paths = pending.get('file_paths', [])
                    title = pending.get('title', 'Instagram Media')
                    username = pending.get('username', '')
                    url = pending.get('url', '')
                    
                    file_count = len(file_paths)
                    status_msg = await u.message.reply_text(f"📤 Uploading {file_count} files...")
                    
                    caption = title
                    if username:
                        caption = f"📱 @{username}\n{title}"
                    
                    file_ids = await send_media_batch(uid, c, file_paths, caption)
                    
                    if file_ids and url:
                        bot_instance.file_id_cache.add(url, file_ids, title, username)
                    
                    await status_msg.delete()
                    
                    for fp in file_paths:
                        Path(fp).unlink(missing_ok=True)
                    
                    bot_instance._pending_downloads[download_ref]['status'] = 'cached'
                    bot_instance._pending_downloads[download_ref]['file_paths'] = []
                    return
                
                elif status in ('pending', 'downloading'):
                    elapsed = int(asyncio.get_event_loop().time() - pending.get('started_at', asyncio.get_event_loop().time()))
                    await u.message.reply_text(
                        f"⏳ Download in progress ({elapsed}s)...\n"
                        f"Tap the link again to check.",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("🔄 Check Again", callback_data=f"check_dl_{download_ref}")
                        ]])
                    )
                    return
                
                elif status == 'failed':
                    error = pending.get('error', 'Unknown error')
                    await u.message.reply_text(f"❌ Download failed: {error}")
                    return
            
            await u.message.reply_text("❌ Download not found. It may have expired.")
            return
        
        elif deep_link == 'cookies':
            if not check_whitelist(uid, bot_instance.config):
                await u.message.reply_text("❌ Not authorized.")
                return
            await bot_instance._ask_cookies(u, c)
            return
    
    if not check_whitelist(uid, bot_instance.config):
        return
    
    await _show_start(bot_instance, u, c, edit=False)

async def _show_start(bot_instance, update, context, edit=False):
    if update.callback_query:
        uid = update.callback_query.from_user.id
        msg = update.callback_query.message
    else:
        uid = update.effective_user.id
        msg = update.message
    
    cookie_status = get_cookie_status_text(uid, bot_instance.cookies, bot_instance.cookie_file_ids)
    reply_markup = bot_instance._admin_menu(uid) if check_admin(uid, bot_instance.config) else bot_instance._menu(uid)
    bot_username = context.bot.username
    
    text = (
        f"👋 Welcome {update.effective_user.first_name}!\n\n"
        "📱 *Instagram Downloader Bot*\n\n"
        "💡 Just send an Instagram link!\n"
        "• Posts → All images/videos\n"
        "• Reels → Video\n"
        "• Stories → Images/videos\n"
        "• Profiles → Profile picture\n\n"
        "🌐 *Inline Mode:*\n"
        f"Type @{bot_username} <link> in any chat!\n\n"
        "👥 *Groups:* Works in groups where\n"
        "a whitelisted user is admin\n\n"
        f"🍪 Cookies: {cookie_status}\n"
        "🔄 Duplicate links use cache\n"
        f"🗑️ Cache: {bot_instance.config.STORAGE_DAYS}d"
    )
    
    if edit:
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
    else:
        await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)