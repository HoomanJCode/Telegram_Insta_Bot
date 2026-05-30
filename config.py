import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_DOWNLOAD_LINK = os.getenv("BASE_DOWNLOAD_LINK", "http://localhost:8000")
WHITELIST_USERS = [int(u) for u in os.getenv("WHITELIST_USERS", "").split(",") if u]
STORAGE_DAYS = int(os.getenv("STORAGE_DAYS", "2"))
MAX_TELEGRAM_FILE_SIZE = int(os.getenv("MAX_TELEGRAM_FILE_SIZE", "52428800"))

DOWNLOAD_DIR = "downloads"
COOKIE_DIR = "data/cookies"
USER_COOKIES_FILE = "data/user_cookies.json"
USER_VIDEOS_FILE = "data/user_videos.json"