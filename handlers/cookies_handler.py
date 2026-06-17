# handlers/cookies_handler.py
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.constants import ParseMode

from utils.helpers import check_whitelist
from core.cookies import validate_cookie_file, create_temp_cookie_file, cleanup_cookie_file

logger = logging.getLogger(__name__)

WAITING_FOR_COOKIES = 1

async def ask_cookies(bot_instance, u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not check_whitelist(u.effective_user.id, bot_instance.config):
        await (u.callback_query.message if u.callback_query else u.message).reply_text("❌ Not authorized.")
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

async def recv_cookies(bot_instance, u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    if not check_whitelist(uid, bot_instance.config):
        return ConversationHandler.END
    
    if not u.message.document:
        await u.message.reply_text("❌ Please send the cookies.txt file.")
        return WAITING_FOR_COOKIES
    
    try:
        doc = u.message.document
        tg_file = await c.bot.get_file(doc.file_id)
        tmp_path = create_temp_cookie_file()
        await tg_file.download_to_drive(tmp_path)
        
        if not validate_cookie_file(tmp_path):
            cleanup_cookie_file(tmp_path)
            await u.message.reply_text(
                "❌ Invalid cookie file. No Instagram cookies found.\n"
                "Make sure you're logged into Instagram and export correctly.")
            return WAITING_FOR_COOKIES
        
        if uid in bot_instance.cookies:
            cleanup_cookie_file(bot_instance.cookies[uid])
        
        bot_instance.cookies[uid] = tmp_path
        bot_instance.cookie_file_ids[uid] = doc.file_id
        from core.cookies import save_cookie_ids
        save_cookie_ids(bot_instance.cookie_file_ids)
        
        await u.message.reply_text(
            "✅ Cookies saved!\n\n"
            "🔒 Cookie content stored in RAM only\n"
            "📎 File reference saved for auto-reload\n"
            "🔄 Cookies auto-load when bot restarts\n\n"
            "Now send any Instagram link to download.\n"
            "Works in private chat and groups!",
            reply_markup=bot_instance._menu(uid))
        return ConversationHandler.END
        
    except Exception as e:
        logger.error(f"Cookie error: {e}")
        await u.message.reply_text("❌ Failed to process cookies.")
        return WAITING_FOR_COOKIES