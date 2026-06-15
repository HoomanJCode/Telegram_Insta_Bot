import re
from config import Config

INSTAGRAM_RE = re.compile(
    r'(https?://)?(www\.)?instagram\.com/('
    r'p/[^/?#\s]+|'
    r'reel/[^/?#\s]+|'
    r'stories/[^/?#\s]+|'
    r'[^/?#\s]+/?$'
    r')'
)

def extract_url(text: str) -> str | None:
    m = INSTAGRAM_RE.search(text)
    if m:
        u = m.group(0)
        if u.startswith('www.'):
            u = 'https://' + u
        elif not u.startswith('http'):
            u = 'https://' + u
        return u.rstrip('/')
    return None

def split_caption(text: str, max_len: int = 1024) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."

def check_whitelist(uid: int, config: Config) -> bool:
    return not config.get_whitelist() or uid in config.get_whitelist()

def check_admin(uid: int, config: Config) -> bool:
    admins = config.get_admins()
    if not admins:
        return False
    return uid in admins

def get_cookie_status_text(uid: int, cookies: dict, cookie_file_ids: dict) -> str:
    if uid in cookies:
        return "✅"
    elif uid in cookie_file_ids:
        return "📎"
    return "❌"