import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

CACHE_FILE = Path('data') / 'file_id_cache.json'

class FileIDCache:
    """Cache Telegram file IDs per URL to prevent re-uploading."""
    
    def __init__(self, storage_days: int):
        self.storage_days = storage_days
        self._cache: Dict[str, dict] = {}
        self._load()
    
    def _load(self):
        try:
            if CACHE_FILE.exists():
                self._cache = json.loads(CACHE_FILE.read_text())
                logger.info(f"Loaded {len(self._cache)} cached file IDs")
        except Exception as e:
            logger.error(f"Cache load error: {e}")
            self._cache = {}
    
    def _save(self):
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(self._cache, indent=2))
        except Exception as e:
            logger.error(f"Cache save error: {e}")
    
    def get(self, url: str) -> Optional[dict]:
        entry = self._cache.get(url)
        if not entry:
            return None
        cached_time = entry.get('cached_time', 0)
        if cached_time:
            age_days = (time.time() - cached_time) / 86400
            if age_days > self.storage_days:
                return None
        return entry
    
    def add(self, url: str, file_ids: List[str], title: str = '', username: str = ''):
        self._cache[url] = {
            'file_ids': file_ids,
            'title': title,
            'username': username,
            'cached_time': time.time(),
        }
        self._save()
        logger.info(f"Cached {len(file_ids)} file IDs for {url[:80]}")
    
    def cleanup_expired(self):
        cutoff = time.time() - (self.storage_days * 86400)
        expired = []
        for url, entry in self._cache.items():
            if entry.get('cached_time', 0) < cutoff:
                expired.append(url)
        for url in expired:
            self._cache.pop(url, None)
        if expired:
            logger.info(f"Cleaned {len(expired)} expired cache entries")
            self._save()
    
    def __len__(self):
        return len(self._cache)