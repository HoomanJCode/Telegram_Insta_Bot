import os
from typing import List
from dotenv import load_dotenv

load_dotenv()

class Config:
    def __init__(self):
        self.BOT_TOKEN = os.getenv('BOT_TOKEN', '')
        self._whitelist = os.getenv('WHITELIST_USERS', '')
        self.STORAGE_DAYS = int(os.getenv('STORAGE_DAYS', '2'))
        self.MAX_TELEGRAM_FILE_SIZE = int(os.getenv('MAX_TELEGRAM_FILE_SIZE', '50')) * 1024 * 1024
    
    def get_whitelist(self) -> List[int]:
        return [int(u) for u in self._whitelist.split(',') if u] if self._whitelist else []