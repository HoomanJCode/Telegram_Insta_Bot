import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DOWNLOADS_DIR = Path('downloads')

def check_gallery_dl():
    try:
        result = subprocess.run(['gallery-dl', '--version'], capture_output=True, text=True)
        logger.info(f"gallery-dl version: {result.stdout.strip()}")
    except FileNotFoundError:
        logger.error("gallery-dl not found! Installing...")
        subprocess.run(['pip', 'install', 'gallery-dl'], check=True)

def get_unique_download_dir(uid: int) -> Path:
    timestamp = int(time.time())
    dir_name = f"{uid}_{timestamp}"
    return DOWNLOADS_DIR / dir_name

def download_media(uid: int, url: str, cookie_path: str) -> tuple:
    """Download Instagram media. Returns (file_paths, title, username)."""
    output_dir = get_unique_download_dir(uid)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        cmd = ['gallery-dl', '--cookies', cookie_path, '--dest', str(output_dir), url]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        
        if result.returncode != 0:
            raise Exception(f"Download failed: {result.stderr[:200]}")
        
        all_files = sorted(
            [str(f) for f in output_dir.rglob('*') if f.is_file()],
            key=lambda x: Path(x).name
        )
        
        if not all_files:
            raise Exception("No files downloaded")
        
        info = get_media_info(uid, url, cookie_path)
        title = info.get('title', 'Instagram Media')
        username = info.get('username', '')
        
        logger.info(f"Downloaded {len(all_files)} files")
        return all_files, title, username
        
    except Exception as e:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise

def get_media_info(uid: int, url: str, cookie_path: str) -> dict:
    """Get media info from gallery-dl JSON output."""
    try:
        cmd = ['gallery-dl', '--cookies', cookie_path, '--dump-json', url]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0:
            data = json.loads(result.stdout)
            if isinstance(data, list) and len(data) > 0:
                first = data[0]
                if isinstance(first, list) and len(first) >= 2:
                    meta = first[1]
                    if isinstance(meta, dict):
                        return {
                            'title': (meta.get('description', '') or 
                                     f"Post by {meta.get('username', 'Unknown')}").strip(),
                            'username': meta.get('username', ''),
                        }
    except:
        pass
    
    return {'title': 'Instagram Media', 'username': ''}