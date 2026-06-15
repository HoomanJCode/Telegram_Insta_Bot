import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

COOKIE_IDS_FILE = Path('data') / 'cookie_file_ids.json'

def load_cookie_ids() -> Dict[int, str]:
    try:
        if COOKIE_IDS_FILE.exists():
            data = json.loads(COOKIE_IDS_FILE.read_text())
            logger.info(f"Loaded {len(data)} cookie file references")
            return {int(k): v for k, v in data.items()}
    except Exception as e:
        logger.error(f"Load cookie IDs error: {e}")
    return {}

def save_cookie_ids(cookie_file_ids: Dict[int, str]):
    try:
        COOKIE_IDS_FILE.parent.mkdir(parents=True, exist_ok=True)
        COOKIE_IDS_FILE.write_text(json.dumps(
            {str(k): v for k, v in cookie_file_ids.items()}, indent=2))
    except Exception as e:
        logger.error(f"Save cookie IDs error: {e}")

def validate_cookie_file(file_path: str) -> bool:
    try:
        with open(file_path, 'r') as f:
            content = f.read()
        return 'instagram.com' in content
    except:
        return False

def create_temp_cookie_file() -> str:
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
    return tmp.name

def cleanup_cookie_file(file_path: str):
    try:
        os.unlink(file_path)
    except:
        pass