import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from media_manager.database import get_async_db
from media_manager.models import CleanupSettings, MediaItem, User
from media_manager.quality import volume_is_healthy
from media_manager.web.api.auth import get_admin_user, get_current_active_user
from media_manager.web.services.cleanup_suggestions import compute_suggestions, get_or_create_settings

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/cleanup",
    tags=["cleanup"],
    # Admin-only: cleanup/migration expose library paths and destructive actions.
    dependencies=[Depends(get_admin_user)],
)

# Suggestions are cheap to compute (same order of cost as the Duplicates
# listing) but change slowly - a short TTL avoids recomputing on every wizard
# step transition within one session without going stale for long.
SUGGESTIONS_CACHE_TTL_SECONDS = 300
_suggestions_cache_data: Optional[dict] = None
_suggestions_cache_timestamp: float = 0


def _clear_suggestions_cache() -> None:
    global _suggestions_cache_data, _suggestions_cache_timestamp
    _suggestions_cache_data = None
    _suggestions_cache_timestamp = 0


class SettingsUpdateRequest(BaseModel):
    never_played_grace_days: int
    stale_after_days: int


class DeleteRequest(BaseModel):
    media_item_ids: List[str]


@router.get("/settings")
async def get_cleanup_settings(
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(get_current_active_user),
):
    movie = await get_or_create_settings(db, "movie")
    tv = await get_or_create_settings(db, "tv")
    return {
        "movie": {
            "never_played_grace_days": movie.never_played_grace_days,
            "stale_after_days": movie.stale_after_days,
        },
        "tv": {
            "never_played_grace_days": tv.never_played_grace_days,
            "stale_after_days": tv.stale_after_days,
        },
    }


@router.put("/settings/{media_type}")
async def update_cleanup_settings(
    media_type: str,
    req: SettingsUpdateRequest,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_db),
):
    if media_type not in ("movie", "tv"):
        raise HTTPException(status_code=400, detail="media_type must be 'movie' or 'tv'")
    if req.never_played_grace_days < 0 or req.stale_after_days < 0:
        raise HTTPException(status_code=400, detail="Thresholds must be non-negative")

    settings = await get_or_create_settings(db, media_type)
    settings.never_played_grace_days = req.never_played_grace_days
    settings.stale_after_days = req.stale_after_days
    await db.commit()
    _clear_suggestions_cache()
    return {"message": f"{media_type} cleanup settings updated successfully"}


@router.get("/suggestions")
async def get_cleanup_suggestions(
    force_refresh: bool = Query(False, description="Force recompute instead of using the cached result"),
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(get_current_active_user),
):
    global _suggestions_cache_data, _suggestions_cache_timestamp
    now = time.time()
    if not force_refresh and _suggestions_cache_data is not None and (now - _suggestions_cache_timestamp) < SUGGESTIONS_CACHE_TTL_SECONDS:
        return {**_suggestions_cache_data, "cached": True}

    data = await compute_suggestions(db)
    _suggestions_cache_data = data
    _suggestions_cache_timestamp = now
    return {**data, "cached": False}


@router.post("/delete")
async def delete_cleanup_selection(
    req: DeleteRequest,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_db),
):
    """
    Deletes the specific MediaItem ids the user selected in the wizard (movies
    and/or whole TV series' episode ids, already flattened by the frontend from
    /suggestions). Same safe-delete mechanics as
    web/api/inventory.py's delete_worse_duplicates: per-volume health check,
    parallel disk removal, then a single bulk DB delete.
    """
    if not req.media_item_ids:
        return {"status": "success", "deleted_count": 0, "freed_bytes": 0, "deleted_files": []}

    items_res = await db.execute(select(MediaItem).where(MediaItem.id.in_(req.media_item_ids)))
    items_to_delete = items_res.scalars().all()
    if not items_to_delete:
        return {"status": "success", "deleted_count": 0, "freed_bytes": 0, "deleted_files": []}

    blocked = [item for item in items_to_delete if not volume_is_healthy(item.storage_volume)]
    if blocked:
        vols = sorted({item.storage_volume for item in blocked if item.storage_volume})
        raise HTTPException(
            status_code=409,
            detail=f"Refusing delete: storage volume(s) look unmounted or empty: {vols}",
        )

    def _remove_file_task(item_path: str):
        if os.path.exists(item_path):
            try:
                os.remove(item_path)
                return True, None
            except Exception as e:
                return False, str(e)
        return True, None

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=8) as executor:
        removal_tasks = [loop.run_in_executor(executor, _remove_file_task, item.path) for item in items_to_delete]
        removal_results = await asyncio.gather(*removal_tasks)

    deleted_ids = []
    deleted_files = []
    freed_bytes = 0
    permission_denied_count = 0

    for item, (success, err_msg) in zip(items_to_delete, removal_results):
        if success:
            deleted_ids.append(item.id)
            deleted_files.append(item.path)
            freed_bytes += item.size_bytes
        else:
            logger.warning(f"Skipping DB removal for {item.path} due to disk error: {err_msg}")
            if "Permission denied" in str(err_msg):
                permission_denied_count += 1

    if deleted_ids:
        await db.execute(delete(MediaItem).where(MediaItem.id.in_(deleted_ids)))
        await db.commit()

    _clear_suggestions_cache()
    from media_manager.web.api.inventory import clear_duplicates_cache
    clear_duplicates_cache()

    return {
        "status": "success",
        "deleted_count": len(deleted_ids),
        "freed_bytes": freed_bytes,
        "permission_denied_count": permission_denied_count,
        "deleted_files": deleted_files,
    }
