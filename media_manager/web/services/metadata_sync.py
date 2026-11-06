import asyncio
import logging
from datetime import datetime, timedelta
from sqlalchemy import select
from media_manager.database import AsyncSessionLocal
from media_manager.models import MediaItem, MetadataCache, MovieCastCache
from media_manager.web.api.integrations import _populate_media_details

logger = logging.getLogger(__name__)

async def run_metadata_sync():
    logger.info("Starting weekly metadata sync background task...")
    try:
        async with AsyncSessionLocal() as db:
            # 1. Fetch all media items
            res = await db.execute(select(MediaItem))
            media_items = res.scalars().all()
            if not media_items:
                logger.info("No media items found in inventory to sync.")
                return

            # Group them by type + title to avoid duplicate queries
            seen = set()
            items_to_sync = []
            for item in media_items:
                mtype = item.media_type.lower()
                title = item.title
                if mtype == "tv":
                    from media_manager.web.api.inventory import get_tv_show_name
                    series_name = get_tv_show_name(item.path)
                    if series_name:
                        title = series_name
                
                key = (mtype, title.lower().strip())
                if key not in seen:
                    seen.add(key)
                    items_to_sync.append({
                        "title": title,
                        "media_type": mtype,
                        "path": item.path if mtype == "tv" else None
                    })

            # 2. Fetch existing cache and cast cache states
            db_res = await db.execute(select(MetadataCache))
            caches = {c.cache_key: c for c in db_res.scalars().all()}

            cast_res = await db.execute(select(MovieCastCache.tmdb_id))
            cast_ids = set(cast_res.scalars().all())

            # Only sync if never cached, cache is older than 7 days, or missing detailed cast details
            now = datetime.utcnow()
            cutoff = now - timedelta(days=7)
            filtered_items = []
            for item in items_to_sync:
                ckey = f"{item['media_type']}:{item['title'].lower().strip()}"
                if ckey not in caches:
                    filtered_items.append(item)
                else:
                    cache_item = caches[ckey]
                    if (cache_item.updated_at < cutoff or 
                        not cache_item.tmdb_id or 
                        cache_item.tmdb_id not in cast_ids):
                        filtered_items.append(item)

            logger.info(f"Syncing metadata for {len(filtered_items)} unique titles (out of {len(items_to_sync)} total)...")
            
            # Process in small chunks to prevent SQLite lock contention
            for chunk_start in range(0, len(filtered_items), 5):
                chunk = filtered_items[chunk_start:chunk_start+5]
                await _populate_media_details(chunk, db)
                await asyncio.sleep(2.0)  # Wait 2 seconds between chunks to allow regular API requests to write/read

            logger.info("Weekly metadata sync completed successfully.")
    except Exception as e:
        logger.error(f"Error during weekly metadata sync: {e}", exc_info=True)

async def weekly_metadata_sync_scheduler():
    # Delay initial sync by 1 hour on startup to prevent lock contention and boot latency
    await asyncio.sleep(3600)
    while True:
        await run_metadata_sync()
        # Sleep for 7 days (604800 seconds)
        await asyncio.sleep(604800)
