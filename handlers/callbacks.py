# handlers/callbacks.py
import logging
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from utils.helpers import check_admin

logger = logging.getLogger(__name__)

async def router(bot_instance, u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    d, uid = q.data, u.effective_user.id
    
    if d == 'b':
        from handlers.start import _show_start
        await _show_start(bot_instance, u, c, edit=True)
    elif d == 'c':
        await bot_instance._ask_cookies(u, c)
    elif d == 'admin_stats':
        await _admin_stats(bot_instance, u, c)
    elif d.startswith('check_dl_'):
        download_ref = d[9:]
        await _check_download(bot_instance, u, c, download_ref)

async def _admin_stats(bot_instance, u, c):
    q = u.callback_query
    uid = u.effective_user.id
    
    if not check_admin(uid, bot_instance.config):
        await q.answer("Admin only", show_alert=True)
        return
    
    await q.message.edit_text(
        f"📊 *Bot Statistics*\n\n"
        f"💾 Cached URLs: {len(bot_instance.file_id_cache)}\n"
        f"👥 Users with cookie refs: {len(bot_instance.cookie_file_ids)}\n"
        f"🍪 Active cookies in RAM: {len(bot_instance.cookies)}\n"
        f"⏳ Pending downloads: {len(bot_instance._pending_downloads)}\n"
        f"🏠 Cached group permissions: {len(bot_instance._allowed_groups)}\n"
        f"🗑️ Storage days: {bot_instance.config.STORAGE_DAYS}\n"
        f"🌐 Inline mode: Enabled\n"
        f"👥 Group mode: Enabled",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=bot_instance._admin_menu(uid))

async def _check_download(bot_instance, u, c, download_ref: str):
    q = u.callback_query
    
    if download_ref in bot_instance._pending_downloads:
        pending = bot_instance._pending_downloads[download_ref]
        status = pending.get('status', 'unknown')
        
        if status in ('ready', 'cached'):
            await q.message.edit_text("✅ Sending media...")
            c.args = [f"dl_{download_ref}"]
            from handlers.start import start_cmd
            await start_cmd(bot_instance, u, c)
            return
        elif status in ('pending', 'downloading'):
            elapsed = int(time.time() - pending.get('started_at', time.time()))
            await q.message.edit_text(
                f"⏳ Still downloading ({elapsed}s)...",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Check Again", callback_data=f"check_dl_{download_ref}")
                ]])
            )
            return
        elif status == 'failed':
            error = pending.get('error', 'Unknown error')
            await q.message.edit_text(f"❌ Failed: {error}")
            return
    
    await q.message.edit_text("❌ Download not found. It may have expired.")