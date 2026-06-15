import asyncio
import logging
from pathlib import Path
from typing import List

from telegram import InputMediaPhoto

logger = logging.getLogger(__name__)

MAX_IMAGES_PER_MEDIA_GROUP = 10

async def send_media_batch(chat_id, context, file_paths: List[str], caption: str, reply_to_message_id: int = None) -> List[str]:
    """Send media files to Telegram. Returns list of file IDs."""
    if not file_paths:
        return []
    
    all_file_ids = []
    images = []
    videos = []
    others = []
    
    for fp in file_paths:
        ext = Path(fp).suffix.lower()
        if ext in ('.jpg', '.jpeg', '.png', '.webp'):
            images.append(fp)
        elif ext in ('.mp4', '.webm', '.mkv'):
            videos.append(fp)
        else:
            others.append(fp)
    
    total_images = len(images)
    if total_images > 0:
        batches = [images[i:i + MAX_IMAGES_PER_MEDIA_GROUP] 
                  for i in range(0, total_images, MAX_IMAGES_PER_MEDIA_GROUP)]
        
        for batch_idx, batch in enumerate(batches):
            media_group = []
            batch_caption = ""
            
            if batch_idx == 0 and caption:
                batch_caption = caption[:1024]
            
            for i, fp in enumerate(batch):
                with open(fp, 'rb') as f:
                    if i == 0 and batch_caption:
                        media_group.append(InputMediaPhoto(media=f, caption=batch_caption))
                    else:
                        media_group.append(InputMediaPhoto(media=f))
            
            if media_group:
                try:
                    sent = await context.bot.send_media_group(
                        chat_id=chat_id,
                        media=media_group,
                        reply_to_message_id=reply_to_message_id,
                        write_timeout=60,
                        read_timeout=60,
                    )
                    for s in sent:
                        if s.photo:
                            all_file_ids.append(s.photo[-1].file_id)
                    logger.info(f"Sent image batch {batch_idx + 1}/{len(batches)}")
                except Exception as e:
                    logger.error(f"Failed to send image batch: {e}")
                    for fp in batch:
                        try:
                            with open(fp, 'rb') as f:
                                s = await context.bot.send_photo(
                                    chat_id=chat_id, photo=f,
                                    reply_to_message_id=reply_to_message_id)
                                all_file_ids.append(s.photo[-1].file_id)
                        except Exception as e2:
                            logger.error(f"Failed to send individual image: {e2}")
            
            if len(batches) > 1:
                await asyncio.sleep(1)
    
    for fp in videos:
        try:
            with open(fp, 'rb') as f:
                s = await context.bot.send_video(
                    chat_id=chat_id,
                    video=f,
                    caption=caption[:1024] if not images else None,
                    supports_streaming=True,
                    reply_to_message_id=reply_to_message_id,
                    write_timeout=60,
                )
                all_file_ids.append(s.video.file_id)
        except Exception as e:
            logger.error(f"Failed to send video: {e}")
    
    for fp in others:
        try:
            with open(fp, 'rb') as f:
                s = await context.bot.send_document(
                    chat_id=chat_id, document=f,
                    reply_to_message_id=reply_to_message_id)
                all_file_ids.append(s.document.file_id)
        except Exception as e:
            logger.error(f"Failed to send document: {e}")
    
    return all_file_ids

async def resend_by_file_ids(chat_id, context, cached_entry: dict, reply_to_message_id: int = None) -> bool:
    """Resend media using cached Telegram file IDs."""
    file_ids = cached_entry.get('file_ids', [])
    title = cached_entry.get('title', '')
    
    if not file_ids:
        return False
    
    try:
        for i, file_id in enumerate(file_ids):
            caption = title[:1024] if i == 0 and title else None
            try:
                await context.bot.send_video(
                    chat_id=chat_id, video=file_id,
                    caption=caption, supports_streaming=True,
                    reply_to_message_id=reply_to_message_id)
            except:
                try:
                    await context.bot.send_photo(
                        chat_id=chat_id, photo=file_id, caption=caption,
                        reply_to_message_id=reply_to_message_id)
                except:
                    try:
                        await context.bot.send_document(
                            chat_id=chat_id, document=file_id, caption=caption,
                            reply_to_message_id=reply_to_message_id)
                    except:
                        return False
            await asyncio.sleep(0.3)
        return True
    except Exception as e:
        logger.error(f"Resend error: {e}")
        return False