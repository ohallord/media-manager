import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from media_manager.database import get_async_db
from media_manager.models import IntegrationConfig, MediaItem, User
from media_manager.playback_matching import parse_path_mappings
from media_manager.web.api.auth import get_admin_user, get_current_active_user
from media_manager.web.services.playback_sync import PLAYBACK_SOURCES, run_playback_sync
from media_manager.web.services.playback_lookup import (
    isoformat as _isoformat,
    load_stats_by_media_item,
    merge_stats,
    sources_with_plays,
)
# Imported as a module so this always reads the pooled client web/app.py's
# lifespan assigns after import time - see playback_sync.py for the same pattern.
from media_manager.web.api import integrations as integrations_module

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/playback",
    tags=["playback"],
    dependencies=[Depends(get_current_active_user)],
)


class PlaybackSourceConfigRequest(BaseModel):
    api_url: str
    api_key: str
    enabled: Optional[bool] = True
    path_mappings: Optional[List[Dict[str, str]]] = None


@router.get("/config")
async def get_playback_source_configs(
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_db),
):
    result = await db.execute(
        select(IntegrationConfig).where(IntegrationConfig.service_name.in_(PLAYBACK_SOURCES.keys()))
    )
    configs = result.scalars().all()
    return {
        "configs": [
            {
                "service_name": c.service_name,
                "api_url": c.api_url,
                "api_key": c.api_key_encrypted,
                "enabled": c.enabled,
                "status": c.status,
                "last_checked_at": _isoformat(c.last_checked_at),
                "path_mappings": parse_path_mappings(c.extra_json),
            }
            for c in configs
        ]
    }


@router.put("/config/{service_name}")
async def update_playback_source_config(
    service_name: str,
    req: PlaybackSourceConfigRequest,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_db),
):
    if service_name not in PLAYBACK_SOURCES:
        raise HTTPException(status_code=400, detail="Invalid service_name")

    extra_json = json.dumps({"path_mappings": req.path_mappings}) if req.path_mappings else None

    result = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == service_name))
    config = result.scalar_one_or_none()

    if not config:
        config = IntegrationConfig(
            service_name=service_name,
            api_url=req.api_url,
            api_key_encrypted=req.api_key,
            enabled=req.enabled,
            status="UNKNOWN",
            extra_json=extra_json,
        )
        db.add(config)
    else:
        config.api_url = req.api_url
        config.api_key_encrypted = req.api_key
        config.enabled = req.enabled
        config.extra_json = extra_json

    await db.commit()
    return {"message": f"{service_name} configuration updated successfully"}


@router.post("/test/{service_name}")
async def test_playback_source_connection(
    service_name: str,
    req: PlaybackSourceConfigRequest,
    admin: User = Depends(get_admin_user),
):
    if service_name not in PLAYBACK_SOURCES:
        raise HTTPException(status_code=400, detail="Invalid service_name")

    client_cls = PLAYBACK_SOURCES[service_name]
    client = client_cls(req.api_url, req.api_key, http_client=integrations_module.SHARED_HTTP_CLIENT)
    healthy = await client.get_status()
    return {"healthy": healthy, "service": service_name}


@router.post("/sync")
async def trigger_playback_sync(
    background_tasks: BackgroundTasks,
    admin: User = Depends(get_admin_user),
):
    background_tasks.add_task(run_playback_sync)
    return {"message": "Playback sync started"}


@router.get("/items")
async def list_playback_items(
    media_type: Optional[str] = Query(None, description="'movie' or 'tv'"),
    unplayed_only: bool = Query(False, description="Only items with a merged play_count of 0"),
    min_days_since_played: Optional[int] = Query(
        None, ge=0, description="Only items never played, or last played at least this many days ago"
    ),
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(get_current_active_user),
):
    """
    Per-movie/per-episode play stats: a merged play_count/last_played_at
    across all configured sources, plus the raw per-source breakdown.
    """
    query = select(MediaItem)
    if media_type:
        query = query.where(MediaItem.media_type == media_type)
    media_items = (await db.execute(query)).scalars().all()
    if not media_items:
        return {"items": [], "total": 0}

    stats_by_item = await load_stats_by_media_item(db, [m.id for m in media_items])
    now = datetime.utcnow()

    results: List[Dict[str, Any]] = []
    for item in media_items:
        stats = stats_by_item.get(item.id, [])
        play_count, last_played_at = merge_stats(stats)

        if unplayed_only and play_count > 0:
            continue
        if min_days_since_played is not None and last_played_at is not None:
            if (now - last_played_at).days < min_days_since_played:
                continue

        results.append({
            "media_item_id": item.id,
            "title": item.title,
            "media_type": item.media_type,
            "season": item.season,
            "episode": item.episode,
            "path": item.path,
            "play_count": play_count,
            "last_played_at": _isoformat(last_played_at),
            "added_at": _isoformat(item.file_created_at),
            "sources": [
                {
                    "source": s.source,
                    "play_count": s.play_count,
                    "last_played_at": _isoformat(s.last_played_at),
                }
                for s in stats
            ],
        })

    # Never-played/oldest-played first, since that's the pruning-relevant order.
    results.sort(key=lambda r: (r["last_played_at"] or "", r["title"]))
    total = len(results)
    return {"items": results[offset:offset + limit], "total": total}


@router.get("/series")
async def list_playback_series(
    min_days_since_played: Optional[int] = Query(
        None, ge=0, description="Only series never played, or last played at least this many days ago"
    ),
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(get_current_active_user),
):
    """
    Per-TV-series rollup (grouped the same way Inventory derives series
    identity - media_manager.web.api.inventory.get_tv_show_name, since there's
    no dedicated series table): episode count, total plays across all
    episodes, most recent play, and how many episodes have never been played
    - the whole-series view a per-episode list can't answer on its own.
    """
    from media_manager.web.api.inventory import get_tv_show_name

    media_items = (await db.execute(select(MediaItem).where(MediaItem.media_type == "tv"))).scalars().all()
    if not media_items:
        return {"series": []}

    stats_by_item = await load_stats_by_media_item(db, [m.id for m in media_items])
    now = datetime.utcnow()

    series_map: Dict[str, Dict[str, Any]] = {}
    for item in media_items:
        show_name = get_tv_show_name(item.path).strip() or item.title
        stats = stats_by_item.get(item.id, [])
        play_count, last_played_at = merge_stats(stats)

        entry = series_map.setdefault(show_name, {
            "show_name": show_name,
            "episode_count": 0,
            "total_plays": 0,
            "last_played_at": None,
            "never_played_episode_count": 0,
            "added_at": None,
            "sources": set(),
        })
        entry["episode_count"] += 1
        entry["total_plays"] += play_count
        if play_count == 0:
            entry["never_played_episode_count"] += 1
        if last_played_at and (not entry["last_played_at"] or last_played_at > entry["last_played_at"]):
            entry["last_played_at"] = last_played_at
        if item.file_created_at and (not entry["added_at"] or item.file_created_at < entry["added_at"]):
            entry["added_at"] = item.file_created_at
        entry["sources"].update(sources_with_plays(stats))

    series_list: List[Dict[str, Any]] = []
    for entry in series_map.values():
        last_played_at = entry["last_played_at"]
        if min_days_since_played is not None and last_played_at is not None:
            if (now - last_played_at).days < min_days_since_played:
                continue
        series_list.append({
            **entry,
            "last_played_at": _isoformat(last_played_at),
            "added_at": _isoformat(entry["added_at"]),
            "sources": sorted(entry["sources"]),
        })

    series_list.sort(key=lambda s: (s["last_played_at"] or "", s["show_name"]))
    return {"series": series_list}
