import asyncio
import os
import time
import logging
from typing import Optional, List, Dict
from fastapi import APIRouter, Depends, Query, BackgroundTasks, HTTPException
from sqlalchemy import select, or_, func
from sqlalchemy.ext.asyncio import AsyncSession

from media_manager.database import get_async_db
from media_manager.models import MediaItem, DuplicateGroup
from media_manager.scanner import scan_all
from media_manager.quality import update_duplicate_groups

router = APIRouter(prefix="/api/v1/inventory", tags=["inventory"])

def get_tv_show_name(path: str) -> str:
    if not path:
        return ""
    parts = path.split('/')
    season_idx = -1
    for idx, p in enumerate(parts):
        if 'season' in p.lower() or 'specials' in p.lower():
            season_idx = idx
            break
    if season_idx > 0:
        return parts[season_idx - 1]
    elif len(parts) >= 2:
        return parts[-2]
    return ""

from media_manager.web.api.auth import get_current_active_user

async def _enrich_inventory_items(items_list: list, db: AsyncSession) -> None:
    """Attach posters/metadata when possible. Never fail the inventory listing itself."""
    try:
        from media_manager.web.api.integrations import _populate_media_details
        await _populate_media_details(items_list, db)
    except Exception:
        logging.exception("Failed to enrich inventory items; returning unenriched list")

    try:
        from media_manager.web.services.playback_lookup import (
            isoformat, load_stats_by_media_item, merge_stats, sources_with_plays
        )
        stats_by_item = await load_stats_by_media_item(db, [i["id"] for i in items_list])
        for item in items_list:
            stats = stats_by_item.get(item["id"], [])
            play_count, last_played_at = merge_stats(stats)
            item["play_count"] = play_count
            item["last_played_at"] = isoformat(last_played_at)
            item["play_sources"] = sources_with_plays(stats)
    except Exception:
        logging.exception("Failed to enrich inventory items with playback stats")

@router.get("/items")
async def list_inventory_items(
    q: Optional[str] = Query(None, description="Search query matching title or path"),
    media_type: Optional[str] = Query(None, description="'movie' or 'tv'"),
    resolution: Optional[str] = Query(None, description="e.g. '4K', '1080p'"),
    audio_codec: Optional[str] = Query(None, description="e.g. 'atmos', 'truehd', 'ac3'"),
    letter: Optional[str] = Query(None, description="Filter by starting letter e.g. 'A', 'B' or '#'"),
    limit: int = Query(100, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_async_db),
    user: get_current_active_user = Depends(get_current_active_user)
):
    """
    Returns paginated media items with technical metadata, type filtering, letter index jumping, and TV show season ordering.
    """
    query = select(MediaItem)

    if q:
        search_pattern = f"%{q.strip()}%"
        query = query.where(
            or_(
                MediaItem.title.ilike(search_pattern),
                MediaItem.path.ilike(search_pattern),
                MediaItem.video_codec.ilike(search_pattern)
            )
        )

    if media_type:
        query = query.where(MediaItem.media_type == media_type.lower())

    if resolution:
        query = query.where(MediaItem.resolution.ilike(f"%{resolution}%"))

    if audio_codec:
        query = query.where(MediaItem.audio_codec.ilike(f"%{audio_codec}%"))

    if letter:
        clean_letter = letter.strip().upper()
        if media_type == 'tv':
            # Execute base TV query
            all_candidates = (await db.execute(query)).scalars().all()
            filtered_items = []
            for item in all_candidates:
                show_name = get_tv_show_name(item.path).strip()
                show_upper = show_name.upper()
                clean = show_upper[4:] if show_upper.startswith('THE ') else show_upper
                if clean_letter == '#':
                    if clean and clean[0].isdigit():
                        filtered_items.append(item)
                else:
                    if clean.startswith(clean_letter):
                        filtered_items.append(item)

            filtered_items.sort(key=lambda x: (get_tv_show_name(x.path).lower(), x.season or 0, x.episode or 0, x.title or ''))
            
            total = len(filtered_items)
            paginated_items = filtered_items[offset:offset + limit]
            items_list = [
                {
                    "id": item.id,
                    "path": item.path,
                    "storage_volume": item.storage_volume,
                    "title": item.title,
                    "media_type": item.media_type,
                    "season": item.season,
                    "episode": item.episode,
                    "resolution": item.resolution,
                    "height": item.height,
                    "width": item.width,
                    "video_codec": item.video_codec,
                    "video_profile": item.video_profile,
                    "bit_depth": item.bit_depth,
                    "hdr_format": item.hdr_format,
                    "video_bitrate": item.video_bitrate,
                    "container": item.container,
                    "duration_seconds": item.duration_seconds,
                    "audio_codec": item.audio_codec,
                    "audio_channels": item.audio_channels,
                    "bitrate": item.bitrate,
                    "size_bytes": item.size_bytes,
                    "quality_score": item.quality_score,
                    "file_created_at": item.file_created_at.isoformat() if item.file_created_at else None,
                    "last_scanned_at": item.last_scanned_at.isoformat() if item.last_scanned_at else None
                }
                for item in paginated_items
            ]
            await _enrich_inventory_items(items_list, db)

            return {
                "total": total,
                "limit": limit,
                "offset": offset,
                "letter": letter,
                "items": items_list
            }

        else:
            if clean_letter == '#':
                query = query.where(
                    or_(
                        func.substr(MediaItem.title, 1, 1).in_([str(i) for i in range(10)]),
                        *[MediaItem.path.ilike(f"%/{i}%") for i in range(10)]
                    )
                )
            elif len(clean_letter) == 1 and clean_letter.isalpha():
                let = clean_letter.upper()
                query = query.where(
                    or_(
                        MediaItem.title.ilike(f"{let}%"),
                        MediaItem.title.ilike(f"The {let}%")
                    )
                )

    # Count total matching for non-tv
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar_one_or_none() or 0

    query = query.order_by(
        MediaItem.title.asc(),
        MediaItem.season.asc().nullsfirst(),
        MediaItem.episode.asc().nullsfirst()
    ).offset(offset).limit(limit)

    result = await db.execute(query)
    items = result.scalars().all()

    items_list = [
        {
            "id": item.id,
            "path": item.path,
            "storage_volume": item.storage_volume,
            "title": item.title,
            "media_type": item.media_type,
            "season": item.season,
            "episode": item.episode,
            "resolution": item.resolution,
            "height": item.height,
            "width": item.width,
            "video_codec": item.video_codec,
            "video_profile": item.video_profile,
            "bit_depth": item.bit_depth,
            "hdr_format": item.hdr_format,
            "video_bitrate": item.video_bitrate,
            "container": item.container,
            "duration_seconds": item.duration_seconds,
            "audio_codec": item.audio_codec,
            "audio_channels": item.audio_channels,
            "bitrate": item.bitrate,
            "size_bytes": item.size_bytes,
            "quality_score": item.quality_score,
            "file_created_at": item.file_created_at.isoformat() if item.file_created_at else None,
            "last_scanned_at": item.last_scanned_at.isoformat() if item.last_scanned_at else None
        }
        for item in items
    ]
    await _enrich_inventory_items(items_list, db)

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "letter": letter,
        "items": items_list
    }

@router.get("/counts")
async def get_inventory_counts(
    db: AsyncSession = Depends(get_async_db),
    user: get_current_active_user = Depends(get_current_active_user)
):
    """
    Library-wide totals for the header count badges: movie file count, and TV
    show count (distinct shows, grouped the same way as the Inventory page's
    season view via get_tv_show_name) + episode count.
    """
    movie_count_result = await db.execute(
        select(func.count()).select_from(MediaItem).where(MediaItem.media_type == "movie")
    )
    movie_count = movie_count_result.scalar_one_or_none() or 0

    tv_paths_result = await db.execute(
        select(MediaItem.path).where(MediaItem.media_type == "tv")
    )
    tv_paths = tv_paths_result.scalars().all()
    tv_episode_count = len(tv_paths)
    tv_show_count = len({get_tv_show_name(path) for path in tv_paths})

    return {
        "movie_count": movie_count,
        "tv_show_count": tv_show_count,
        "tv_episode_count": tv_episode_count
    }

# Duplicates Caching System (24h TTL = 86,400s)
DUPLICATES_CACHE_TIMESTAMP: float = 0
DUPLICATES_CACHE_DATA: Optional[dict] = None

def clear_duplicates_cache():
    global DUPLICATES_CACHE_TIMESTAMP, DUPLICATES_CACHE_DATA
    DUPLICATES_CACHE_TIMESTAMP = 0
    DUPLICATES_CACHE_DATA = None

@router.get("/duplicates")
async def list_duplicate_groups(
    force_refresh: bool = Query(False, description="Force recompute duplicates"),
    db: AsyncSession = Depends(get_async_db),
    user: get_current_active_user = Depends(get_current_active_user)
):
    """
    Returns identified duplicate groups ranked by quality score with 24h caching.
    Automatically invalidates on file deletion or manual refresh.
    """
    global DUPLICATES_CACHE_TIMESTAMP, DUPLICATES_CACHE_DATA
    now = time.time()

    # 24-hour cache check (86,400s)
    if not force_refresh and DUPLICATES_CACHE_DATA is not None and (now - DUPLICATES_CACHE_TIMESTAMP) < 86400:
        return {**DUPLICATES_CACHE_DATA, "cached": True, "cache_tier": "memory"}

    query = select(DuplicateGroup).order_by(DuplicateGroup.updated_at.desc())
    result = await db.execute(query)
    groups = result.scalars().all()

    # One batched query for every group's members instead of one query per group.
    group_ids = [g.id for g in groups]
    candidates_by_group: Dict[str, List[MediaItem]] = {}
    if group_ids:
        all_candidates_query = (
            select(MediaItem)
            .where(MediaItem.duplicate_group_id.in_(group_ids))
            .order_by(MediaItem.duplicate_group_id, MediaItem.quality_score.desc())
        )
        all_cand_result = await db.execute(all_candidates_query)
        for item in all_cand_result.scalars().all():
            candidates_by_group.setdefault(item.duplicate_group_id, []).append(item)

    response_groups = []
    total_worse_files = 0
    total_reclaimable_bytes = 0

    for g in groups:
        candidates = candidates_by_group.get(g.id, [])

        if not candidates or len(candidates) < 2:
            continue

        best_item = candidates[0]
        worse_candidates = candidates[1:]

        total_worse_files += len(worse_candidates)
        total_reclaimable_bytes += sum(c.size_bytes for c in worse_candidates)

        response_groups.append({
            "id": g.id,
            "media_title": g.media_title,
            "season_episode": g.season_episode,
            "total_count": len(candidates),
            "best_item": {
                "id": best_item.id,
                "path": best_item.path,
                "resolution": best_item.resolution,
                "video_codec": best_item.video_codec,
                "audio_codec": best_item.audio_codec,
                "quality_score": best_item.quality_score,
                "size_bytes": best_item.size_bytes,
                "recommendation": "KEEP"
            },
            "worse_items": [
                {
                    "id": c.id,
                    "path": c.path,
                    "resolution": c.resolution,
                    "video_codec": c.video_codec,
                    "audio_codec": c.audio_codec,
                    "quality_score": c.quality_score,
                    "size_bytes": c.size_bytes,
                    "recommendation": "DELETE"
                }
                for c in worse_candidates
            ],
            "candidates": [
                {
                    "id": c.id,
                    "path": c.path,
                    "resolution": c.resolution,
                    "video_codec": c.video_codec,
                    "audio_codec": c.audio_codec,
                    "quality_score": c.quality_score,
                    "size_bytes": c.size_bytes,
                    "recommendation": "DELETE"
                }
                for c in worse_candidates
            ]
        })

    data = {
        "groups": response_groups,
        "total_groups": len(response_groups),
        "total_worse_files": total_worse_files,
        "total_reclaimable_bytes": total_reclaimable_bytes,
        "cached": False,
        "cache_tier": "computed"
    }

    DUPLICATES_CACHE_DATA = data
    DUPLICATES_CACHE_TIMESTAMP = now
    return data

from media_manager.web.api.auth import get_admin_user

@router.delete("/items/{item_id}")
async def delete_inventory_item(
    item_id: str, 
    db: AsyncSession = Depends(get_async_db),
    admin_user: get_admin_user = Depends(get_admin_user)
):
    """
    Deletes a specific media item file from disk and database instantly, then invalidates duplicate cache.
    Requires Admin authorization.
    """
    res = await db.execute(select(MediaItem).where(MediaItem.id == item_id))
    item = res.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Media item not found")

    file_path = item.path
    from media_manager.quality import volume_is_healthy
    if not volume_is_healthy(item.storage_volume):
        raise HTTPException(
            status_code=409,
            detail="Storage volume looks unmounted or empty; refusing to delete this file or its inventory row.",
        )
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
            logging.info(f"Successfully deleted file from disk: {file_path}")
        except PermissionError as pe:
            logging.error(f"Permission denied deleting file {file_path}: {pe}")
            raise HTTPException(
                status_code=403, 
                detail=f"Permission denied on disk for '{os.path.basename(file_path)}'. The folder '/media/Movies' is owned by root."
            )
        except Exception as e:
            logging.error(f"Failed to remove file from disk {file_path}: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to delete file: {e}")

    await db.delete(item)
    await db.commit()

    # Purge duplicate cache instantly
    clear_duplicates_cache()

    return {"status": "success", "deleted_item_id": item_id, "deleted_path": file_path}

@router.post("/duplicates/delete-worse")
async def delete_worse_duplicates(
    group_id: Optional[str] = Query("all", description="Group ID or 'all'"),
    db: AsyncSession = Depends(get_async_db),
    admin_user: get_admin_user = Depends(get_admin_user)
):
    """
    Deletes recommended worse duplicate files (lower quality score) in parallel for maximum speed.
    Instantly purges duplicate cache. Requires Admin authorization.
    """
    from concurrent.futures import ThreadPoolExecutor
    import asyncio
    from sqlalchemy import delete

    if group_id == "all":
        groups_res = await db.execute(select(DuplicateGroup))
        groups = groups_res.scalars().all()
    else:
        groups_res = await db.execute(select(DuplicateGroup).where(DuplicateGroup.id == group_id))
        groups = groups_res.scalars().all()

    items_to_delete = []
    group_ids = [g.id for g in groups]
    if group_ids:
        all_cand_res = await db.execute(
            select(MediaItem)
            .where(MediaItem.duplicate_group_id.in_(group_ids))
            .order_by(MediaItem.duplicate_group_id, MediaItem.quality_score.desc())
        )
        candidates_by_group: Dict[str, List[MediaItem]] = {}
        for item in all_cand_res.scalars().all():
            candidates_by_group.setdefault(item.duplicate_group_id, []).append(item)
        for g in groups:
            candidates = candidates_by_group.get(g.id, [])
            if len(candidates) > 1:
                items_to_delete.extend(candidates[1:])

    if not items_to_delete:
        return {"status": "success", "deleted_count": 0, "freed_bytes": 0, "deleted_files": []}

    from media_manager.quality import volume_is_healthy
    blocked = [item for item in items_to_delete if not volume_is_healthy(item.storage_volume)]
    if blocked:
        vols = sorted({item.storage_volume for item in blocked if item.storage_volume})
        raise HTTPException(
            status_code=409,
            detail=f"Refusing delete-worse: storage volume(s) look unmounted or empty: {vols}",
        )

    # Parallel file removal on disk
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
            logging.warning(f"Skipping DB removal for {item.path} due to disk error: {err_msg}")
            if "Permission denied" in str(err_msg):
                permission_denied_count += 1

    # Bulk SQL batch delete from database
    if deleted_ids:
        await db.execute(delete(MediaItem).where(MediaItem.id.in_(deleted_ids)))
        await db.commit()

    # Instant cache purge
    clear_duplicates_cache()

    return {
        "status": "success",
        "deleted_count": len(deleted_ids),
        "freed_bytes": freed_bytes,
        "permission_denied_count": permission_denied_count,
        "deleted_files": deleted_files
    }

@router.post("/scan")
async def trigger_inventory_scan(
    background_tasks: BackgroundTasks,
    user=Depends(get_admin_user),
):
    """
    Triggers an asynchronous media directory scan in the background.
    Admin only: a full scan walks every media directory.
    """
    clear_duplicates_cache()
    background_tasks.add_task(scan_all)
    return {"status": "scan_initiated", "message": "Background scan of media directories started."}
