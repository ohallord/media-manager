import os
import hashlib
import io
import json
import time
import asyncio
import httpx
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple
from urllib.parse import quote, unquote, urlparse
from PIL import Image
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from media_manager.database import get_async_db, AsyncSessionLocal
from media_manager.models import IntegrationConfig, User, MediaItem, ArtworkCache, MetadataCache, MonitoredDirectory, MovieCastCache, AppLogEvent, ServiceHeartbeat
from media_manager.web.api.auth import get_current_active_user, get_admin_user
from media_manager.integrations.radarr import RadarrClient
from media_manager.integrations.sonarr import SonarrClient
from media_manager.integrations.sabnzbd import SABnzbdClient
from media_manager.integrations.fanart import FanartClient
from media_manager.integrations.tmdb import TMDbClient
from media_manager.integrations.syncthing import SyncthingClient

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/integrations",
    tags=["integrations"],
    dependencies=[Depends(get_current_active_user)],
)

# Set by web/app.py's lifespan to one pooled httpx.AsyncClient shared by every
# Radarr/Sonarr/SABnzbd/TMDb/Fanart/Syncthing client constructed below, instead of
# each opening a fresh client (and TCP/TLS handshake) per call. Stays None outside
# the running app (tests, standalone scripts), in which case each client falls back
# to its old per-call ephemeral client - see BaseHTTPClient.
SHARED_HTTP_CLIENT: Optional[httpx.AsyncClient] = None

# In-memory fast artwork cache for 0ms sub-millisecond responses
IN_MEMORY_ARTWORK_CACHE: Dict[str, Dict[str, Any]] = {}
IN_MEMORY_METADATA_CACHE: Dict[str, Dict[str, Any]] = {}

# Upcoming Movies feature caches. The discover list and Radarr's own library both
# change over time (unlike artwork/metadata, which are effectively permanent), so
# these are time-boxed rather than cached forever.
UPCOMING_DISCOVER_CACHE_TTL_SECONDS = 6 * 3600
RADARR_LIBRARY_CACHE_TTL_SECONDS = 120
SONARR_LIBRARY_CACHE_TTL_SECONDS = 120
IN_MEMORY_UPCOMING_DISCOVER_CACHE: Dict[Tuple[str, str], Dict[str, Any]] = {}
IN_MEMORY_UPCOMING_TV_DISCOVER_CACHE: Dict[Tuple[str, str], Dict[str, Any]] = {}
IN_MEMORY_RADARR_LIBRARY_CACHE: Dict[str, Any] = {}
IN_MEMORY_SONARR_LIBRARY_CACHE: Dict[str, Any] = {}
IN_MEMORY_CAST_CACHE: Dict[int, List[Dict[str, Any]]] = {}
IN_MEMORY_RELEASE_INFO_CACHE: Dict[int, List[Dict[str, Any]]] = {}
IN_MEMORY_TV_WATCH_PROVIDERS_CACHE: Dict[int, List[str]] = {}
IN_MEMORY_MOVIE_WATCH_PROVIDERS_CACHE: Dict[int, List[str]] = {}
IN_MEMORY_TV_NETWORKS_CACHE: Dict[int, List[str]] = {}
IN_MEMORY_TV_EXTERNAL_IDS_CACHE: Dict[int, Optional[int]] = {}
IN_MEMORY_TVDB_TO_TMDB_CACHE: Dict[int, Optional[int]] = {}

# Trending shifts daily, so it gets a much shorter TTL than the discover caches above.
TRENDING_CACHE_TTL_SECONDS = 3600
IN_MEMORY_TRENDING_CACHE: Dict[Tuple[str, str], Dict[str, Any]] = {}
DISCOVERY_RESULT_LIMIT = 40
# Ingress/nginx typically 504s around 60s. Per-call httpx timeouts do not always
# fire on stalled DNS, so batch TMDb fan-out must be time-boxed as a whole.
TMDB_BATCH_TIMEOUT_SECONDS = 12.0
TMDB_BATCH_CONCURRENCY = 8
TRENDING_ENRICH_TIMEOUT_SECONDS = 20.0
# Search hits Radarr/Sonarr then TMDb enrichment. Broad queries (e.g. "batman")
# can exceed ingress timeouts and return HTML 502/504, which the SPA used to
# JSON.parse. Keep the whole remote+enrich path under ~30s.
SEARCH_REMOTE_TIMEOUT_SECONDS = 12.0
SEARCH_LIBRARY_IDS_TIMEOUT_SECONDS = 8.0
SEARCH_ENRICH_TIMEOUT_SECONDS = 18.0

# Same short-lived pattern as RADARR/SONARR_LIBRARY_CACHE - needs to reflect a user's
# own just-submitted request fairly quickly, but doesn't need to be instant.
REQUEST_USER_MAPPINGS_CACHE_TTL_SECONDS = 120
IN_MEMORY_REQUEST_USER_MAPPINGS_CACHE: Dict[str, Any] = {}

def find_local_artwork(file_path: str) -> Dict[str, Optional[str]]:
    """
    Scans the directory of a local media file for local artwork image files (poster.jpg, backdrop.jpg, logo.png, etc.).
    """
    if not file_path or not os.path.exists(file_path):
        return {"poster": None, "background": None, "logo": None, "banner": None}

    file_dir = os.path.dirname(file_path) if os.path.isfile(file_path) else file_path
    parent_dir = os.path.dirname(file_dir)

    search_dirs = [file_dir]
    if parent_dir and parent_dir != file_dir and os.path.exists(parent_dir):
        search_dirs.append(parent_dir)

    found = {"poster": None, "background": None, "logo": None, "banner": None}

    poster_names = ["poster.jpg", "poster.png", "folder.jpg", "folder.png", "cover.jpg", "cover.png"]
    backdrop_names = ["backdrop.jpg", "backdrop.png", "fanart.jpg", "fanart.png", "background.jpg", "background.png"]
    logo_names = ["logo.png", "clearlogo.png", "clearart.png", "logo.jpg"]
    banner_names = ["banner.jpg", "banner.png"]

    basestem = os.path.splitext(os.path.basename(file_path))[0] if os.path.isfile(file_path) else ""

    for d in search_dirs:
        try:
            entries = os.listdir(d)
            entries_lower = {e.lower(): e for e in entries}

            if not found["poster"]:
                for p in poster_names:
                    if p.lower() in entries_lower:
                        found["poster"] = os.path.join(d, entries_lower[p.lower()])
                        break
                if not found["poster"] and basestem:
                    for ext in [".jpg", ".png", "-poster.jpg", "-poster.png"]:
                        cand = f"{basestem}{ext}".lower()
                        if cand in entries_lower:
                            found["poster"] = os.path.join(d, entries_lower[cand])
                            break

            if not found["background"]:
                for b in backdrop_names:
                    if b.lower() in entries_lower:
                        found["background"] = os.path.join(d, entries_lower[b.lower()])
                        break

            if not found["logo"]:
                for l in logo_names:
                    if l.lower() in entries_lower:
                        found["logo"] = os.path.join(d, entries_lower[l.lower()])
                        break

            if not found["banner"]:
                for bn in banner_names:
                    if bn.lower() in entries_lower:
                        found["banner"] = os.path.join(d, entries_lower[bn.lower()])
                        break
        except Exception:
            pass

    return found

class UpdateIntegrationRequest(BaseModel):
    api_url: str
    api_key: str
    enabled: Optional[bool] = True

class AddMediaRequest(BaseModel):
    type: str  # 'movie' or 'tv'
    media_data: Dict[str, Any]
    quality_profile_id: Optional[int] = None
    root_folder_path: Optional[str] = None
    language_profile_id: Optional[int] = None
    monitored: Optional[bool] = True
    search_now: Optional[bool] = True
    season_monitor: Optional[str] = None  # Sonarr: "all" or "firstSeason"

@router.get("/config")
async def get_integration_configs(
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_db)
):
    result = await db.execute(select(IntegrationConfig))
    configs = result.scalars().all()
    return {
        "configs": [
            {
                "id": c.id,
                "service_name": c.service_name,
                "api_url": c.api_url,
                "api_key": c.api_key_encrypted,
                "enabled": c.enabled,
                "status": c.status,
                "last_checked_at": c.last_checked_at.isoformat() + "Z" if c.last_checked_at else None
            }
            for c in configs
        ]
    }

@router.put("/config/{service_name}")
async def update_integration_config(
    service_name: str,
    req: UpdateIntegrationRequest,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_db)
):
    if service_name not in ["radarr", "sonarr", "sabnzbd", "fanart", "tmdb", "email", "syncthing"]:
        raise HTTPException(status_code=400, detail="Invalid service_name")

    default_url = "smtp.example.com:587:you@example.com:true" if service_name == "email" else ("https://api.themoviedb.org/3" if service_name == "tmdb" else ("https://webservice.fanart.tv/v3" if service_name == "fanart" else ("http://localhost:8384" if service_name == "syncthing" else "")))

    result = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == service_name))
    config = result.scalar_one_or_none()

    if not config:
        config = IntegrationConfig(
            service_name=service_name,
            api_url=req.api_url or default_url,
            api_key_encrypted=req.api_key,
            enabled=req.enabled,
            status="HEALTHY",
            last_checked_at=datetime.utcnow()
        )
        db.add(config)
    else:
        config.api_url = req.api_url or default_url
        config.api_key_encrypted = req.api_key
        config.enabled = req.enabled
        config.status = "HEALTHY"
        config.last_checked_at = datetime.utcnow()

    await db.commit()
    return {"message": f"{service_name} configuration updated successfully"}

@router.post("/test/{service_name}")
async def test_integration_connection(
    service_name: str,
    req: UpdateIntegrationRequest,
    admin: User = Depends(get_admin_user)
):
    """
    Tests connection to Radarr, Sonarr, SABnzbd, Fanart.tv, or TMDb and returns status.
    """
    if service_name == "radarr":
        client = RadarrClient(req.api_url, req.api_key, http_client=SHARED_HTTP_CLIENT)
        healthy = await client.get_status()
        profiles = await client.get_profiles() if healthy else []
        folders = await client.get_root_folders() if healthy else []
        return {
            "healthy": healthy,
            "service": "radarr",
            "profiles": [{"id": p.get("id"), "name": p.get("name")} for p in profiles],
            "root_folders": [{"id": f.get("id"), "path": f.get("path")} for f in folders]
        }
    elif service_name == "sonarr":
        client = SonarrClient(req.api_url, req.api_key, http_client=SHARED_HTTP_CLIENT)
        healthy = await client.get_status()
        profiles = await client.get_profiles() if healthy else []
        folders = await client.get_root_folders() if healthy else []
        return {
            "healthy": healthy,
            "service": "sonarr",
            "profiles": [{"id": p.get("id"), "name": p.get("name")} for p in profiles],
            "root_folders": [{"id": f.get("id"), "path": f.get("path")} for f in folders]
        }
    elif service_name == "sabnzbd":
        client = SABnzbdClient(req.api_url, req.api_key, http_client=SHARED_HTTP_CLIENT)
        healthy = await client.get_status()
        return {"healthy": healthy, "service": "sabnzbd"}
    elif service_name == "syncthing":
        client = SyncthingClient(req.api_url, req.api_key, http_client=SHARED_HTTP_CLIENT)
        healthy = await client.get_status()
        return {"healthy": healthy, "service": "syncthing"}
    elif service_name == "fanart":
        client = FanartClient(req.api_url or "https://webservice.fanart.tv/v3", req.api_key, http_client=SHARED_HTTP_CLIENT)
        healthy = await client.get_status()
        return {"healthy": healthy, "service": "fanart"}
    elif service_name == "tmdb":
        client = TMDbClient(req.api_url or "https://api.themoviedb.org/3", req.api_key, http_client=SHARED_HTTP_CLIENT)
        healthy = await client.get_status()
        return {"healthy": healthy, "service": "tmdb"}
    elif service_name == "email":
        from media_manager.web.services.email import parse_smtp_config, smtp_connect

        smtp_cfg = parse_smtp_config(req.api_url)
        if not smtp_cfg:
            return {"healthy": False, "service": "email", "error": "Invalid configuration format. Expected 'host:port:sender:use_tls'"}
        try:
            def connect_smtp():
                server = smtp_connect(smtp_cfg, timeout=5)
                if req.api_key:
                    server.login(smtp_cfg.sender, req.api_key)
                server.quit()
                return True

            loop = asyncio.get_event_loop()
            healthy = await loop.run_in_executor(None, connect_smtp)
            return {"healthy": healthy, "service": "email"}
        except Exception as e:
            return {"healthy": False, "service": "email", "error": str(e)}
    else:
        raise HTTPException(status_code=400, detail="Invalid service")

@router.get("/search")
async def search_media_local_and_remote(
    term: str = Query(..., min_length=2),
    type: str = Query("movie", description="'movie' or 'tv'"),
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(get_current_active_user)
):
    """
    Local-First Media Search:
    1. Checks local database for matching media items.
    2. If matching local files exist, returns local items.
    3. Queries Radarr (movies) or Sonarr (tv) ONLY for items NOT already in local library.
    """
    clean_term = term.strip()
    search_type = type.lower() if type in ["movie", "tv"] else "movie"

    # Auto-detect media type if term clearly indicates TV show (e.g., S01E02 or Season 1)
    if "season" in clean_term.lower() or "s0" in clean_term.lower() or "s1" in clean_term.lower() or "episode" in clean_term.lower():
        search_type = "tv"

    # Step 1: Search local inventory database
    local_query = select(MediaItem).where(
        or_(
            MediaItem.title.ilike(f"%{clean_term}%"),
            MediaItem.path.ilike(f"%{clean_term}%")
        )
    )
    if search_type in ["movie", "tv"]:
        local_query = local_query.where(MediaItem.media_type == search_type)

    local_result = await db.execute(local_query)
    local_items = local_result.scalars().all()

    formatted_local_items = [
        {
            "id": item.id,
            "title": item.title,
            "path": item.path,
            "media_type": item.media_type,
            "resolution": item.resolution,
            "quality_score": item.quality_score,
            "size_bytes": item.size_bytes,
            "season": item.season,
            "episode": item.episode
        }
        for item in local_items
    ]

    local_titles_clean = {item.title.lower().strip() for item in local_items}

    # Step 2: Query Radarr (for movies) or Sonarr (for TV shows) ONLY for missing items
    remote_results = []
    service_name = "radarr" if search_type == "movie" else "sonarr"

    result = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == service_name))
    config = result.scalar_one_or_none()

    if config and config.enabled:
        raw_remote = []
        try:
            if search_type == "movie":
                client = RadarrClient(config.api_url, config.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
                raw_remote = await asyncio.wait_for(
                    client.search_movies(clean_term),
                    timeout=SEARCH_REMOTE_TIMEOUT_SECONDS,
                )
            else:
                client = SonarrClient(config.api_url, config.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
                raw_remote = await asyncio.wait_for(
                    client.search_series(clean_term),
                    timeout=SEARCH_REMOTE_TIMEOUT_SECONDS,
                )
        except asyncio.TimeoutError:
            logger.warning("Remote %s search timed out for %r", service_name, clean_term)
        except Exception as e:
            logger.warning("Remote %s search failed for %r: %s", service_name, clean_term, e)

        # Filter out remote results that already exist in local library
        for remote in raw_remote[:20]:
            remote_title = (remote.get("title") or "").lower().strip()
            
            exists_locally = any(
                lt in remote_title or remote_title in lt 
                for lt in local_titles_clean
            )
            
            # ONLY return Radarr or Sonarr results if there ISN'T a local copy already
            if not exists_locally:
                remote_results.append(remote)

    # Step 3: Format final response cards
    processed_results = []

    # If local items exist, return local server results
    if formatted_local_items:
        local_grouped = {}
        for l_item in formatted_local_items:
            t_key = l_item["title"]
            if t_key not in local_grouped:
                local_grouped[t_key] = []
            local_grouped[t_key].append(l_item)

        # Retrieve library items to resolve tmdbId / tvdbId for local items
        library_items = []
        try:
            if search_type == "movie":
                radarr_cfg = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "radarr"))
                r_c = radarr_cfg.scalar_one_or_none()
                if r_c and r_c.enabled:
                    r_client = RadarrClient(r_c.api_url, r_c.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
                    library_items = await asyncio.wait_for(
                        r_client.get_movies(),
                        timeout=SEARCH_LIBRARY_IDS_TIMEOUT_SECONDS,
                    )
            else:
                sonarr_cfg = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "sonarr"))
                s_c = sonarr_cfg.scalar_one_or_none()
                if s_c and s_c.enabled:
                    s_client = SonarrClient(s_c.api_url, s_c.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
                    library_items = await asyncio.wait_for(
                        s_client.get_series(),
                        timeout=SEARCH_LIBRARY_IDS_TIMEOUT_SECONDS,
                    )
        except Exception as e:
            logger.warning(f"Failed to fetch library items for local search matching: {e}")

        for g_title, g_items in local_grouped.items():
            # Try to match the local title with Radarr / Sonarr library items
            matching_item = next((item for item in library_items if item.get("title", "").lower() == g_title.lower()), None)
            
            tmdb_id = matching_item.get("tmdbId") if matching_item and search_type == "movie" else None
            tvdb_id = matching_item.get("tvdbId") if matching_item and search_type == "tv" else None
            
            media_data = None
            if matching_item:
                media_data = {
                    "title": g_title,
                    "tmdbId": tmdb_id,
                    "tvdbId": tvdb_id
                }

            processed_results.append({
                "title": g_title,
                "type": search_type,
                "year": "Local",
                "overview": "Available on local server media inventory.",
                "status": "Available Locally",
                "in_local_library": True,
                "local_items": g_items,
                "media_data": media_data
            })

    # Query request user mappings if current user is admin
    tmdb_req_map, tvdb_req_map = {}, {}
    if getattr(user, "role", None) == "admin":
        tmdb_req_map, tvdb_req_map, _ = await _get_request_user_mappings(db)

    # Fetch library IDs to check if they are already added
    radarr_tmdb_ids = set()
    sonarr_tvdb_ids = set()
    try:
        radarr_tmdb_ids = await asyncio.wait_for(
            _get_radarr_tmdb_ids(db),
            timeout=SEARCH_LIBRARY_IDS_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("Radarr library ID lookup timed out during search")
    try:
        sonarr_tvdb_ids = await asyncio.wait_for(
            _get_sonarr_tvdb_ids(db),
            timeout=SEARCH_LIBRARY_IDS_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("Sonarr library ID lookup timed out during search")

    # Add Radarr / Sonarr results for missing items
    for remote in remote_results:
        req_by = None
        if getattr(user, "role", None) == "admin":
            if search_type == "movie":
                req_by = tmdb_req_map.get(remote.get("tmdbId"))
            else:
                req_by = tvdb_req_map.get(remote.get("tvdbId"))

        in_radarr = False
        in_sonarr = False
        if search_type == "movie":
            tmdb_id = remote.get("tmdbId")
            if tmdb_id and tmdb_id in radarr_tmdb_ids:
                in_radarr = True
        else:
            tvdb_id = remote.get("tvdbId")
            if tvdb_id and tvdb_id in sonarr_tvdb_ids:
                in_sonarr = True

        status = "in_radarr" if in_radarr else ("in_sonarr" if in_sonarr else "not_added")

        processed_results.append({
            "title": remote.get("title"),
            "type": search_type,
            "year": remote.get("year"),
            "overview": remote.get("overview") or "No overview available.",
            "status": status,
            "in_local_library": False,
            "local_items": [],
            "media_data": remote,
            "requested_by": req_by
        })

    if processed_results:
        try:
            await asyncio.wait_for(
                _populate_media_details(processed_results, db),
                timeout=SEARCH_ENRICH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Search enrichment timed out for %r (%s results); returning titles without extra metadata",
                clean_term,
                len(processed_results),
            )

    return {
        "term": term,
        "type": search_type,
        "service_queried": service_name,
        "has_local_matches": len(formatted_local_items) > 0,
        "local_count": len(formatted_local_items),
        "results": processed_results
    }

async def _resolve_add_defaults(client, service_name: str, quality_profile_id: Optional[int], root_folder_path: Optional[str]):
    """
    Fills in an unset quality profile / root folder using whatever Radarr/Sonarr already
    has configured, so the caller never has to supply a directory or profile manually.
    """
    if quality_profile_id is None:
        profiles = await client.get_profiles()
        if not profiles:
            raise HTTPException(status_code=400, detail=f"No quality profiles configured in {service_name.capitalize()}. Please set one up there first.")
        quality_profile_id = profiles[0].get("id")

    folders = await client.get_root_folders()
    if not folders:
        raise HTTPException(status_code=400, detail=f"No root folder configured in {service_name.capitalize()}. Please set one up there first.")
    if not root_folder_path:
        root_folder_path = folders[0].get("path")
    else:
        # Only accept a root folder the *arr already has; never an arbitrary path.
        allowed = {str(f.get("path") or "").rstrip("/") for f in folders}
        if root_folder_path.rstrip("/") not in allowed:
            raise HTTPException(status_code=400, detail=f"Unknown {service_name.capitalize()} root folder.")

    return quality_profile_id, root_folder_path

async def _resolve_language_profile_id(client: "SonarrClient", language_profile_id: Optional[int]) -> Optional[int]:
    """
    Fills in an unset language profile from whatever Sonarr already has configured.
    Sonarr v3 rejects series without a valid languageProfileId, while v4 dropped
    language profiles entirely (no endpoint), so a lookup failure just means "not
    applicable here" rather than an error worth surfacing.
    """
    if language_profile_id is not None:
        return language_profile_id
    try:
        profiles = await client.get_language_profiles()
    except Exception as e:
        logger.warning(f"Sonarr get_language_profiles failed, omitting languageProfileId: {e}")
        return None
    return profiles[0].get("id") if profiles else None

async def _add_movie_via_radarr(
    client: RadarrClient,
    movie_data: Dict[str, Any],
    monitored: bool,
    search_now: bool,
    quality_profile_id: Optional[int] = None,
    root_folder_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Adds a movie to Radarr, resolving an unset quality profile / root folder from
    what Radarr already has configured, and retrying as a search if it's already added.
    """
    quality_profile_id, root_folder_path = await _resolve_add_defaults(
        client, "radarr", quality_profile_id, root_folder_path
    )
    res = await client.add_movie(
        movie_data=movie_data,
        quality_profile_id=quality_profile_id,
        root_folder_path=root_folder_path,
        monitored=monitored,
        search_now=search_now
    )
    if not res.get("success") and "already been added" in str(res.get("error")):
        # Retrieve existing movie ID from Radarr
        movies = await client.get_movies()
        tmdb_id = movie_data.get("tmdbId")
        existing = next((m for m in movies if m.get("tmdbId") == tmdb_id), None)
        if existing:
            movie_id = existing.get("id")
            # Trigger search/redownload command
            cmd_res = await client.trigger_command("MoviesSearch", movieIds=[movie_id])
            if cmd_res.get("success"):
                return {"success": True, "message": f"Movie '{existing.get('title')}' is already in Radarr. Triggered search to download/redownload it."}
    return res

async def _add_series_via_sonarr(
    client: SonarrClient,
    series_data: Dict[str, Any],
    monitored: bool,
    search_now: bool,
    quality_profile_id: Optional[int] = None,
    root_folder_path: Optional[str] = None,
    language_profile_id: Optional[int] = None,
    season_monitor: Optional[str] = None
) -> Dict[str, Any]:
    """
    Adds a series to Sonarr, resolving an unset quality profile / root folder / language
    profile from what Sonarr already has configured, and retrying as a search if it's
    already added.
    """
    quality_profile_id, root_folder_path = await _resolve_add_defaults(
        client, "sonarr", quality_profile_id, root_folder_path
    )
    language_profile_id = await _resolve_language_profile_id(client, language_profile_id)
    res = await client.add_series(
        series_data=series_data,
        quality_profile_id=quality_profile_id,
        root_folder_path=root_folder_path,
        language_profile_id=language_profile_id,
        monitored=monitored,
        search_now=search_now,
        season_monitor=season_monitor
    )
    if not res.get("success") and "already been added" in str(res.get("error")):
        # Retrieve existing series ID from Sonarr
        series_list = await client.get_series()
        tvdb_id = series_data.get("tvdbId")
        existing = next((s for s in series_list if s.get("tvdbId") == tvdb_id), None)
        if existing:
            series_id = existing.get("id")
            # Trigger search command for missing episodes
            cmd_res = await client.trigger_command("SeriesSearch", seriesId=series_id)
            if cmd_res.get("success"):
                return {"success": True, "message": f"Series '{existing.get('title')}' is already in Sonarr. Triggered search for missing episodes."}
    return res

@router.post("/add")
async def add_media_to_arr(
    req: AddMediaRequest,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    Adds a movie or show directly into Radarr or Sonarr with profile and root folder.
    """
    service_name = "radarr" if req.type == "movie" else "sonarr"
    result = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == service_name))
    config = result.scalar_one_or_none()

    if not config or not config.enabled:
        raise HTTPException(status_code=400, detail=f"{service_name.capitalize()} is not configured.")

    if req.type == "movie":
        client = RadarrClient(config.api_url, config.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
        res = await _add_movie_via_radarr(
            client, req.media_data, req.monitored, req.search_now,
            req.quality_profile_id, req.root_folder_path
        )
        if res.get("success") or "already in Radarr" in res.get("message", ""):
            title = req.media_data.get("title", "Unknown Movie")
            tmdb_id = req.media_data.get("tmdbId", "Unknown")
            username = getattr(user, "username", None) or (user.get("username") if isinstance(user, dict) else "Unknown")
            msg = f"User '{username}' requested movie '{title}' (TMDb: {tmdb_id}) via Radarr"
            logger.info(f"[MEDIA_REQUEST] {msg}")
            db.add(AppLogEvent(
                level="INFO",
                event_tag="[MEDIA_REQUEST]",
                message=msg,
                context_json=json.dumps({"username": username, "tmdb_id": tmdb_id, "title": title})
            ))
            try:
                cache_key = f"{req.type}:{title.lower().strip()}"
                meta_res = await db.execute(select(MetadataCache).where(MetadataCache.cache_key == cache_key))
                cached = meta_res.scalar_one_or_none()
                if cached:
                    cached.requested_by = username
                else:
                    db.add(MetadataCache(
                        cache_key=cache_key,
                        title=title,
                        media_type=req.type,
                        requested_by=username,
                        source="none"
                    ))
            except Exception as e:
                logger.warning(f"Failed to update MetadataCache requested_by: {e}")
            await db.commit()
            IN_MEMORY_RADARR_LIBRARY_CACHE.clear()
        return res
    else:
        client = SonarrClient(config.api_url, config.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
        res = await _add_series_via_sonarr(
            client, req.media_data, req.monitored, req.search_now,
            req.quality_profile_id, req.root_folder_path, req.language_profile_id,
            season_monitor=req.season_monitor
        )

        if res.get("success") or "already in Sonarr" in res.get("message", ""):
            title = req.media_data.get("title", "Unknown Show")
            tvdb_id = req.media_data.get("tvdbId", "Unknown")
            username = getattr(user, "username", None) or (user.get("username") if isinstance(user, dict) else "Unknown")
            msg = f"User '{username}' requested TV show '{title}' (TVDb: {tvdb_id}) via Sonarr"
            logger.info(f"[MEDIA_REQUEST] {msg}")
            db.add(AppLogEvent(
                level="INFO",
                event_tag="[MEDIA_REQUEST]",
                message=msg,
                context_json=json.dumps({"username": username, "tvdb_id": tvdb_id, "title": title})
            ))
            try:
                cache_key = f"{req.type}:{title.lower().strip()}"
                meta_res = await db.execute(select(MetadataCache).where(MetadataCache.cache_key == cache_key))
                cached = meta_res.scalar_one_or_none()
                if cached:
                    cached.requested_by = username
                else:
                    db.add(MetadataCache(
                        cache_key=cache_key,
                        title=title,
                        media_type=req.type,
                        requested_by=username,
                        source="none"
                    ))
            except Exception as e:
                logger.warning(f"Failed to update MetadataCache requested_by: {e}")
            await db.commit()
            IN_MEMORY_SONARR_LIBRARY_CACHE.clear()
        return res

class SearchTriggerRequest(BaseModel):
    type: str  # "movie" or "show"
    tmdb_id: Optional[int] = None
    tvdb_id: Optional[int] = None

@router.post("/search-trigger")
async def trigger_media_search(
    req: SearchTriggerRequest,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    Triggers a search command on Radarr or Sonarr for an already added media item.
    """
    service_name = "radarr" if req.type == "movie" else "sonarr"
    result = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == service_name))
    config = result.scalar_one_or_none()

    if not config or not config.enabled:
        raise HTTPException(status_code=400, detail=f"{service_name.capitalize()} is not configured.")

    if req.type == "movie":
        if not req.tmdb_id:
            raise HTTPException(status_code=400, detail="tmdb_id is required for movie search trigger.")
        client = RadarrClient(config.api_url, config.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
        movies = await client.get_movies()
        existing = next((m for m in movies if m.get("tmdbId") == req.tmdb_id), None)
        if not existing:
            raise HTTPException(status_code=404, detail="Movie not found in Radarr.")
        
        cmd_res = await client.trigger_command("MoviesSearch", movieIds=[existing.get("id")])
        if cmd_res.get("success") or cmd_res.get("id") or cmd_res.get("status") == "started":
            return {"success": True, "message": f"Successfully triggered search for '{existing.get('title')}' in Radarr."}
        return {"success": False, "error": cmd_res.get("error", "Failed to trigger search command.")}
    else:
        if not req.tvdb_id:
            raise HTTPException(status_code=400, detail="tvdb_id is required for show search trigger.")
        client = SonarrClient(config.api_url, config.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
        series_list = await client.get_series()
        existing = next((s for s in series_list if s.get("tvdbId") == req.tvdb_id), None)
        if not existing:
            raise HTTPException(status_code=404, detail="Series not found in Sonarr.")
        
        cmd_res = await client.trigger_command("SeriesSearch", seriesId=existing.get("id"))
        if cmd_res.get("success") or cmd_res.get("id") or cmd_res.get("status") == "started":
            return {"success": True, "message": f"Successfully triggered search for '{existing.get('title')}' in Sonarr."}
        return {"success": False, "error": cmd_res.get("error", "Failed to trigger search command.")}

class AddByTmdbRequest(BaseModel):
    tmdb_id: int
    monitored: Optional[bool] = True
    search_now: Optional[bool] = True
    quality_profile_id: Optional[int] = None

class AddTvByTmdbRequest(BaseModel):
    tmdb_id: int
    monitored: Optional[bool] = True
    search_now: Optional[bool] = True
    quality_profile_id: Optional[int] = None
    season_monitor: Optional[str] = None

# Minimum TMDb popularity score for an upcoming release to be surfaced. Without this,
# theatrical discovery is dominated by obscure one-screen/festival titles (dozens per
# window with near-zero popularity) that bury the handful of releases people actually
# want to track. Chosen empirically: it comfortably keeps wide/notable releases while
# dropping the long tail of unpopular ones.
UPCOMING_MOVIES_MIN_POPULARITY = 5.0

def _parse_explicit_date_range(start_date: Optional[str], end_date: Optional[str]) -> Optional[Tuple[str, str]]:
    """
    Validates an explicit start_date/end_date pair (YYYY-MM-DD, start <= end) used by
    the Calendar view's month navigation, which needs an arbitrary month rather than a
    fixed lookahead-from-today window. Returns None if either is missing/invalid, so
    the caller can fall back to its own default window.
    """
    if isinstance(start_date, str) and isinstance(end_date, str) and start_date and end_date:
        try:
            parsed_start = datetime.strptime(start_date, "%Y-%m-%d").date()
            parsed_end = datetime.strptime(end_date, "%Y-%m-%d").date()
            if parsed_start <= parsed_end:
                return parsed_start.strftime("%Y-%m-%d"), parsed_end.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None

def _resolve_upcoming_date_range(days: int, start_date: Optional[str], end_date: Optional[str]) -> Tuple[str, str]:
    """
    Resolves the (start_date, end_date) window for the upcoming-movies/upcoming-tv
    endpoints: an explicit start_date/end_date pair takes precedence over `days` when
    valid, otherwise falls back to the usual "today + days" window.
    """
    explicit = _parse_explicit_date_range(start_date, end_date)
    if explicit:
        return explicit

    if days not in (30, 60, 90, 120):
        days = 30
    today = datetime.utcnow().date()
    return today.strftime("%Y-%m-%d"), (today + timedelta(days=days)).strftime("%Y-%m-%d")

async def _cached_discover(cache, start_date: str, end_date: str, load) -> Dict[str, Any]:
    """TTL cache of a discover call plus its genre map, keyed by the date window."""
    cache_key = (start_date, end_date)
    cached = cache.get(cache_key)
    now = time.time()
    if cached and (now - cached["fetched_at"]) < UPCOMING_DISCOVER_CACHE_TTL_SECONDS:
        return cached
    raw_results, genre_map = await load()
    entry = {
        "fetched_at": now,
        "start_date": start_date,
        "end_date": end_date,
        "raw_results": raw_results,
        "genre_map": genre_map,
    }
    cache[cache_key] = entry
    return entry

async def _get_cached_discover_results(client: TMDbClient, start_date: str, end_date: str) -> Dict[str, Any]:
    """
    Caches TMDb's discover/genre results per (start_date, end_date) window for
    UPCOMING_DISCOVER_CACHE_TTL_SECONDS so flipping between day-range tabs, revisiting
    a month in the Calendar view, or repeat page loads doesn't re-hit TMDb every time.
    """
    async def load():
        return await client.discover_upcoming_movies(start_date, end_date), await client.get_movie_genres()
    return await _cached_discover(IN_MEMORY_UPCOMING_DISCOVER_CACHE, start_date, end_date, load)

async def _get_cached_discover_tv_results(client: TMDbClient, start_date: str, end_date: str) -> Dict[str, Any]:
    """Same caching shape as _get_cached_discover_results, for TV series premieres."""
    async def load():
        return await client.discover_upcoming_tv(start_date, end_date), await client.get_tv_genres()
    return await _cached_discover(IN_MEMORY_UPCOMING_TV_DISCOVER_CACHE, start_date, end_date, load)

async def _get_radarr_tmdb_ids(db: AsyncSession) -> set:
    """
    Caches the set of tmdbIds already present in Radarr for RADARR_LIBRARY_CACHE_TTL_SECONDS
    — short-lived since it needs to reflect the user's own recent adds fairly quickly.
    """
    now = time.time()
    cached = IN_MEMORY_RADARR_LIBRARY_CACHE
    if cached and (now - cached.get("fetched_at", 0)) < RADARR_LIBRARY_CACHE_TTL_SECONDS:
        return cached["tmdb_ids"]

    radarr_res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "radarr"))
    radarr_cfg = radarr_res.scalar_one_or_none()
    tmdb_ids = set()
    if radarr_cfg and radarr_cfg.enabled:
        radarr_client = RadarrClient(radarr_cfg.api_url, radarr_cfg.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
        radarr_movies = await radarr_client.get_movies()
        tmdb_ids = {m.get("tmdbId") for m in radarr_movies if m.get("tmdbId")}

    IN_MEMORY_RADARR_LIBRARY_CACHE.clear()
    IN_MEMORY_RADARR_LIBRARY_CACHE.update({"fetched_at": now, "tmdb_ids": tmdb_ids})
    return tmdb_ids

async def _get_sonarr_tvdb_ids(db: AsyncSession) -> set:
    """
    Caches the set of tvdbIds already present in Sonarr for SONARR_LIBRARY_CACHE_TTL_SECONDS.
    """
    now = time.time()
    cached = IN_MEMORY_SONARR_LIBRARY_CACHE
    if cached and (now - cached.get("fetched_at", 0)) < SONARR_LIBRARY_CACHE_TTL_SECONDS:
        return cached["tvdb_ids"]

    sonarr_res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "sonarr"))
    sonarr_cfg = sonarr_res.scalar_one_or_none()
    tvdb_ids = set()
    if sonarr_cfg and sonarr_cfg.enabled:
        sonarr_client = SonarrClient(sonarr_cfg.api_url, sonarr_cfg.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
        sonarr_series = await sonarr_client.get_series()
        tvdb_ids = {s.get("tvdbId") for s in sonarr_series if s.get("tvdbId")}

    IN_MEMORY_SONARR_LIBRARY_CACHE.clear()
    IN_MEMORY_SONARR_LIBRARY_CACHE.update({"fetched_at": now, "tvdb_ids": tvdb_ids})
    return tvdb_ids

_EMPTY_CAST_TRAILER = {"cast": [], "trailer_url": None}


def _limit_discover_results(filtered: List[Dict[str, Any]], limit: int = DISCOVERY_RESULT_LIMIT) -> List[Dict[str, Any]]:
    ranked = sorted(filtered, key=lambda m: m.get("popularity") or 0, reverse=True)
    return ranked[:limit]


async def _bounded_id_fetches(
    missing_ids: List[int],
    fetch_one,
    *,
    timeout: Optional[float] = None,
    concurrency: Optional[int] = None,
    label: str = "TMDb batch",
) -> List[Any]:
    """Run per-id TMDb fetches with a concurrency cap and an overall deadline.

    On timeout, return whatever completed and skip the rest (empty/default at
    the caller) instead of hanging until the reverse proxy returns HTML 504.
    """
    if timeout is None:
        timeout = TMDB_BATCH_TIMEOUT_SECONDS
    if concurrency is None:
        concurrency = TMDB_BATCH_CONCURRENCY
    if not missing_ids:
        return []
    sem = asyncio.Semaphore(concurrency)

    async def bounded(tid: int):
        async with sem:
            return await fetch_one(tid)

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[bounded(tid) for tid in missing_ids], return_exceptions=True),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(f"{label} timed out for {len(missing_ids)} ID(s); returning partial results")
        return []

    completed = []
    for item in results:
        if isinstance(item, BaseException):
            logger.warning(f"{label} item failed: {item}")
            continue
        completed.append(item)
    return completed


async def _get_cast_and_trailer(client: TMDbClient, tmdb_ids: List[int], db: AsyncSession, media_type: str = "movie", include_bios: bool = True) -> Dict[int, Dict[str, Any]]:
    """
    Returns {tmdb_id: {"cast": [{name, character, photo_url}, ...], "trailer_url": str|None}}
    (top 5 billed cast + best YouTube trailer link) for the given IDs. Checked in-memory
    first, then the persistent MovieCastCache table, and only hits TMDb (concurrently, for
    whatever's still missing) as a last resort — this data rarely changes once set, so
    it's cached indefinitely like the existing artwork/metadata caches.
    """
    result: Dict[int, Dict[str, Any]] = {}
    missing_ids = [tid for tid in tmdb_ids if tid not in IN_MEMORY_CAST_CACHE]
    for tid in tmdb_ids:
        if tid in IN_MEMORY_CAST_CACHE:
            result[tid] = IN_MEMORY_CAST_CACHE[tid]

    if not missing_ids:
        return result

    db_res = await db.execute(select(MovieCastCache).where(MovieCastCache.tmdb_id.in_(missing_ids)))
    cached_rows = {row.tmdb_id: row for row in db_res.scalars().all()}
    still_missing = []
    for tid in missing_ids:
        row = cached_rows.get(tid)
        if row and row.cast_json is not None:
            entry = {"cast": json.loads(row.cast_json), "trailer_url": row.trailer_url}
            IN_MEMORY_CAST_CACHE[tid] = entry
            result[tid] = entry
        else:
            still_missing.append(tid)

    if still_missing:
        try:
            if media_type == "movie":
                details_list = await asyncio.wait_for(
                    asyncio.gather(*[client.get_movie_details(tid) for tid in still_missing]), timeout=15.0
                )
            else:
                details_list = await asyncio.wait_for(
                    asyncio.gather(*[client.get_tv_details(tid) for tid in still_missing]), timeout=15.0
                )
        except asyncio.TimeoutError:
            # ponytail: each TMDb call declares its own 10s httpx timeout, but that
            # doesn't reliably fire if the stall is in DNS resolution rather than the
            # request itself - async cancellation can't kill a blocked resolver
            # thread, only stop waiting on it (observed live: concurrent /person/{id}
            # calls completing exactly ~30s apart under load instead of together).
            # Skip caching so this batch retries in full next request instead of
            # baking in a permanent "no cast/trailer" result. Real fix: a shared,
            # connection-pooled TMDbClient instead of a fresh httpx.AsyncClient per
            # call, or addressing the cluster DNS flakiness directly.
            logger.warning(f"TMDb detail fetch timed out for {len(still_missing)} {media_type} ID(s); skipping cast/trailer for this batch")
            return result

        per_tid_cast: Dict[int, List[Dict[str, Any]]] = {}
        per_tid_trailer: Dict[int, Optional[str]] = {}
        bio_targets = []  # (cast_entry, person_id) pairs, fetched concurrently below
        for tid, details in zip(still_missing, details_list):
            cast = []
            if details:
                raw_cast = [c for c in details.get("credits", {}).get("cast", [])[:5] if c.get("name")]
                for c in raw_cast:
                    entry = {
                        "name": c.get("name"),
                        "character": c.get("character"),
                        "photo_url": f"https://image.tmdb.org/t/p/w185{c['profile_path']}" if c.get("profile_path") else None,
                        "bio": ""
                    }
                    cast.append(entry)
                    if include_bios and c.get("id"):
                        bio_targets.append((entry, c["id"]))
            per_tid_cast[tid] = cast
            per_tid_trailer[tid] = client.extract_trailer_url(details)

        if bio_targets:
            # This fan-out is up to 5x still_missing (5 cast members per title) -
            # the biggest batch in this function, and the one actually observed
            # stalling live. Bios are cosmetic tooltip text, so a timeout here just
            # leaves them blank rather than discarding the cast/trailer data above.
            try:
                person_details_list = await asyncio.wait_for(
                    asyncio.gather(*[client.get_person_details(person_id) for _, person_id in bio_targets]),
                    timeout=15.0
                )
                for (cast_entry, _), person_details in zip(bio_targets, person_details_list):
                    if person_details:
                        cast_entry["bio"] = person_details.get("biography") or ""
            except asyncio.TimeoutError:
                logger.warning(f"TMDb person-bio fetch timed out for {len(bio_targets)} cast member(s); leaving bios blank")

        pending_rows: List[MovieCastCache] = []
        for tid in still_missing:
            cast = per_tid_cast[tid]
            trailer_url = per_tid_trailer[tid]
            entry = {"cast": cast, "trailer_url": trailer_url}
            IN_MEMORY_CAST_CACHE[tid] = entry
            result[tid] = entry
            pending_rows.append(MovieCastCache(tmdb_id=tid, cast_json=json.dumps(cast), trailer_url=trailer_url))
        if pending_rows:
            try:
                for row in pending_rows:
                    db.add(row)
                await db.commit()
            except Exception as e:
                logger.warning(f"Failed to persist cast/trailer cache batch: {e}")
                await db.rollback()

    return result

async def _cached_id_batch(cache, tmdb_ids, fetch, label):
    results = {tid: cache[tid] for tid in tmdb_ids if tid in cache}
    missing_ids = [tid for tid in tmdb_ids if tid not in cache]
    if missing_ids:
        for tid, value in await _bounded_id_fetches(missing_ids, fetch, label=label):
            cache[tid] = value
            results[tid] = value
    return results

async def _get_release_info_batch(client: TMDbClient, tmdb_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    """
    Fetches release dates for a batch of TMDb IDs and returns a dictionary
    mapping tmdb_id to a list of release date entries (containing type, date, note).
    """
    async def fetch_releases(tid):
        try:
            async with client._client(timeout=8.0) as http_client:
                res = await http_client.get(
                    f"{client.api_url}/movie/{tid}/release_dates",
                    headers=client.headers,
                    params=client.params
                )
                if res.status_code == 200:
                    data = res.json()
                    for r in data.get("results", []):
                        if r.get("iso_3166_1") == "US":
                            return tid, r.get("release_dates", [])
        except Exception as e:
            logger.warning(f"Error fetching release dates for movie {tid}: {e}")
        return tid, []

    return await _cached_id_batch(IN_MEMORY_RELEASE_INFO_CACHE, tmdb_ids, fetch_releases, "TMDb release dates")

async def _get_tv_external_ids_batch(client: TMDbClient, tmdb_ids: List[int]) -> Dict[int, Optional[int]]:
    """
    Fetches (and caches indefinitely, like release info) {tmdb_id: tvdb_id} for a
    batch of TV shows - needed to hand a TMDb-discovered show off to Sonarr, which
    is keyed by TVDB ID rather than TMDb ID.
    """
    async def fetch_one(tid):
        ext = await client.get_tv_external_ids(tid)
        return tid, (ext.get("tvdb_id") if ext else None)

    return await _cached_id_batch(IN_MEMORY_TV_EXTERNAL_IDS_CACHE, tmdb_ids, fetch_one, "TMDb TV external IDs")

async def _get_tv_watch_providers_batch(client: TMDbClient, tmdb_ids: List[int]) -> Dict[int, List[str]]:
    """
    Fetches (and caches indefinitely, like release info) current US streaming
    provider names for a batch of TV shows.
    """
    async def fetch_one(tid):
        return tid, await client.get_tv_watch_providers(tid)

    return await _cached_id_batch(IN_MEMORY_TV_WATCH_PROVIDERS_CACHE, tmdb_ids, fetch_one, "TMDb TV watch providers")

async def _get_movie_watch_providers_batch(client: TMDbClient, tmdb_ids: List[int]) -> Dict[int, List[str]]:
    """
    Fetches (and caches indefinitely, like _get_tv_watch_providers_batch) current US
    streaming provider names for a batch of movies.
    """
    async def fetch_one(tid):
        return tid, await client.get_movie_watch_providers(tid)

    return await _cached_id_batch(IN_MEMORY_MOVIE_WATCH_PROVIDERS_CACHE, tmdb_ids, fetch_one, "TMDb movie watch providers")

async def _get_tv_networks_batch(client: TMDbClient, tmdb_ids: List[int]) -> Dict[int, List[str]]:
    """
    Fetches (and caches indefinitely) each TV show's original broadcast/streaming
    network (e.g. "Apple TV+", "Netflix", "AMC") from TMDb's full details endpoint.
    Distinct from _get_tv_watch_providers_batch: that reflects *current* streaming
    subscription availability, which is correctly empty for a not-yet-aired show -
    this reflects the announced network, the useful "where's this airing" signal
    for not-yet-released titles (Coming Soon, upcoming Trending).
    """
    async def fetch_one(tid):
        details = await client.get_tv_details(tid)
        networks = [n.get("name") for n in (details or {}).get("networks", []) if n.get("name")]
        return tid, networks

    return await _cached_id_batch(IN_MEMORY_TV_NETWORKS_CACHE, tmdb_ids, fetch_one, "TMDb TV networks")

def _safe_json_loads(raw: Any, default: Any = None) -> Any:
    """Parse a cache JSON text column; never raise on corrupt or already-decoded values."""
    if default is None:
        default = []
    if raw is None or raw == "":
        return default
    if isinstance(raw, (list, dict)):
        return raw
    if not isinstance(raw, str):
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return default


async def _populate_media_details(items: List[Dict[str, Any]], db: AsyncSession):
    """
    Populates list of media dictionaries with their cached/TMDb metadata:
    genres, rating, poster/backdrop URLs, top-5 cast (with biography), and trailers.
    """
    tmdb_res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "tmdb"))
    tmdb_cfg = tmdb_res.scalar_one_or_none()
    tmdb_client = None
    if tmdb_cfg and tmdb_cfg.enabled and tmdb_cfg.api_key_encrypted:
        tmdb_client = TMDbClient(tmdb_cfg.api_url, tmdb_cfg.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)

    if not items:
        return

    tmdb_req_map, tvdb_req_map, title_req_map = await _get_request_user_mappings(db)

    def get_type_and_title(it):
        mtype = it.get("media_type") or it.get("type") or "movie"
        mtype = mtype.lower()
        if mtype not in ["movie", "tv"]:
            mtype = "movie"
        title = it.get("title")
        if mtype == "tv" and it.get("path"):
            from media_manager.web.api.inventory import get_tv_show_name
            series_title = get_tv_show_name(it["path"])
            if series_title:
                title = series_title
        return mtype, title

    keys = []
    for it in items:
        mtype, mtitle = get_type_and_title(it)
        if mtitle:
            keys.append(f"{mtype}:{mtitle.lower().strip()}")

    cache_map = {}
    if keys:
        db_res = await db.execute(select(MetadataCache).where(MetadataCache.cache_key.in_(keys)))
        cache_map = {row.cache_key: row for row in db_res.scalars().all()}

    tmdb_ids_to_query = []
    for it in items:
        mtype, mtitle = get_type_and_title(it)
        if mtitle:
            ckey = f"{mtype}:{mtitle.lower().strip()}"
            if ckey in cache_map:
                tid = cache_map[ckey].tmdb_id
                if tid:
                    tmdb_ids_to_query.append(tid)

    cast_map = {}
    if tmdb_ids_to_query:
        cast_res = await db.execute(select(MovieCastCache).where(MovieCastCache.tmdb_id.in_(tmdb_ids_to_query)))
        cast_map = {row.tmdb_id: row for row in cast_res.scalars().all()}

    # Bounds the whole enrichment pass below, not each item's own httpx timeout: those
    # don't reliably fire if the stall is in DNS resolution rather than the request
    # itself (see _get_cast_and_trailer), so N cache-miss items in a row could
    # otherwise stack up well past any reasonable response time. Items reached after
    # the deadline just render without cast/metadata for this request.
    enrich_deadline = time.time() + 20.0

    for it in items:
        try:
            if time.time() > enrich_deadline:
                logger.warning("_populate_media_details enrichment deadline hit with items still unenriched; returning partial results")
                break

            mtype, mtitle = get_type_and_title(it)
            if not mtitle:
                continue
            ckey = f"{mtype}:{mtitle.lower().strip()}"

            meta = None
            if ckey in IN_MEMORY_METADATA_CACHE:
                meta = IN_MEMORY_METADATA_CACHE[ckey]
            elif ckey in cache_map:
                row = cache_map[ckey]
                meta = {
                    "tmdb_id": row.tmdb_id,
                    "overview": row.overview,
                    "rating": row.rating,
                    "genres": _safe_json_loads(row.genres_json, []),
                    "poster_path": row.poster_path,
                    "backdrop_path": row.backdrop_path,
                }
                IN_MEMORY_METADATA_CACHE[ckey] = meta
            elif tmdb_client:
                try:
                    meta_res = await asyncio.wait_for(
                        _get_media_metadata_impl(mtitle, mtype, None, None, None, db), timeout=15.0
                    )
                    meta = meta_res.get("metadata")
                except asyncio.TimeoutError:
                    logger.warning(f"TMDb metadata fetch timed out for '{mtitle}'; skipping for this item")
                except Exception as e:
                    logger.error(f"Error fetching metadata for {mtitle}: {e}")

            if meta:
                it["overview"] = meta.get("overview") or it.get("overview", "")
                it["poster_url"] = meta.get("poster_path")
                it["backdrop_url"] = meta.get("backdrop_path")
                it["rating"] = meta.get("rating")
                it["genres"] = meta.get("genres") or []
                it["tmdb_id"] = meta.get("tmdb_id")

            tid = it.get("tmdb_id")
            if tid:
                cast_trail = None
                if tid in IN_MEMORY_CAST_CACHE:
                    cast_trail = IN_MEMORY_CAST_CACHE[tid]
                elif tid in cast_map:
                    row = cast_map[tid]
                    cast_trail = {
                        "cast": _safe_json_loads(row.cast_json, []),
                        "trailer_url": row.trailer_url
                    }
                    IN_MEMORY_CAST_CACHE[tid] = cast_trail
                elif tmdb_client:
                    try:
                        res = await _get_cast_and_trailer(tmdb_client, [tid], db, media_type=mtype)
                        cast_trail = res.get(tid)
                    except Exception as e:
                        logger.error(f"Error fetching cast/trailer for tmdb_id {tid}: {e}")

                if cast_trail:
                    it["cast"] = cast_trail.get("cast") or []
                    it["trailer_url"] = cast_trail.get("trailer_url")
                    if meta:
                        meta["cast"] = cast_trail.get("cast") or []
                        meta["trailer_url"] = cast_trail.get("trailer_url")

                providers_cache = IN_MEMORY_TV_WATCH_PROVIDERS_CACHE if mtype == "tv" else IN_MEMORY_MOVIE_WATCH_PROVIDERS_CACHE
                if tid in providers_cache:
                    it["streaming_providers"] = providers_cache[tid]
                elif tmdb_client:
                    try:
                        providers = await (
                            tmdb_client.get_tv_watch_providers(tid) if mtype == "tv"
                            else tmdb_client.get_movie_watch_providers(tid)
                        )
                        providers_cache[tid] = providers
                        it["streaming_providers"] = providers
                    except Exception as e:
                        logger.error(f"Error fetching watch providers for tmdb_id {tid}: {e}")

                if mtype == "tv":
                    if tid in IN_MEMORY_TV_NETWORKS_CACHE:
                        it["networks"] = IN_MEMORY_TV_NETWORKS_CACHE[tid]
                    elif tmdb_client:
                        try:
                            networks_map = await _get_tv_networks_batch(tmdb_client, [tid])
                            it["networks"] = networks_map.get(tid, [])
                        except Exception as e:
                            logger.error(f"Error fetching networks for tmdb_id {tid}: {e}")

            # Resolve requested_by user mapping
            req_by = None
            tid = it.get("tmdb_id")
            if tid and tid in tmdb_req_map:
                req_by = tmdb_req_map[tid]
            elif mtitle:
                req_by = title_req_map.get(mtitle.lower().strip())
            it["requested_by"] = req_by
        except Exception as e:
            logger.error(f"Error enriching inventory item {it.get('title')}: {e}")

def _match_streaming_service(note_lower: str) -> List[str]:
    """Best-effort mapping of a TMDb release note to a known streaming service name."""
    if "netflix" in note_lower:
        return ["Netflix"]
    if "apple tv" in note_lower or "apple+" in note_lower:
        return ["Apple TV+"]
    if "disney" in note_lower:
        return ["Disney+"]
    if "hulu" in note_lower:
        return ["Hulu"]
    if "paramount" in note_lower:
        return ["Paramount+"]
    if "hbo max" in note_lower or "max" == note_lower:
        return ["Max"]
    if "amazon" in note_lower or "prime video" in note_lower:
        return ["Amazon Prime Video"]
    if "peacock" in note_lower:
        return ["Peacock"]
    return []

@router.get("/upcoming-movies")
async def get_upcoming_movies(
    days: int = Query(30, description="Lookahead window: 30, 60, 90, or 120 days"),
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD - overrides `days` with an explicit range (used by the Calendar view's month navigation)"),
    end_date: Optional[str] = Query(None, description="YYYY-MM-DD - paired with start_date"),
    db: AsyncSession = Depends(get_async_db)
):
    """
    Lists movies with a confirmed US release date (via TMDb's discover/movie and
    release_dates endpoints) in the next 30/60/90/120 days, prioritizing theatrical
    releases over streaming/digital ones, with top-5 cast headshots and a status
    flag against the local library and Radarr. Excludes movies whose only release
    in this window is a streaming/digital one if they already had a US theatrical
    release before the window started (e.g. a movie that opened in theaters months
    ago and is now just landing on a streaming service) - only that title's first
    US release is ever "coming soon". An explicit start_date/end_date pair (instead
    of days) lets the Calendar view request an arbitrary month directly rather than
    always being anchored to a "today + N days" lookahead window.
    """
    if days not in (30, 60, 90, 120):
        days = 30
    req_start_date, req_end_date = _resolve_upcoming_date_range(days, start_date, end_date)

    tmdb_res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "tmdb"))
    tmdb_cfg = tmdb_res.scalar_one_or_none()
    if not tmdb_cfg or not tmdb_cfg.enabled or not tmdb_cfg.api_key_encrypted:
        raise HTTPException(status_code=400, detail="TMDb is not configured. Add a TMDb API key in Admin settings first.")

    client = TMDbClient(tmdb_cfg.api_url, tmdb_cfg.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)

    discover_cache = await _get_cached_discover_results(client, req_start_date, req_end_date)
    raw_results = discover_cache["raw_results"]
    genre_map = discover_cache["genre_map"]
    start_date = discover_cache["start_date"]
    end_date = discover_cache["end_date"]

    local_result = await db.execute(select(MediaItem.title).where(MediaItem.media_type == "movie"))
    local_titles = {t.lower().strip() for (t,) in local_result.all()}

    radarr_tmdb_ids = await _get_radarr_tmdb_ids(db)
    tmdb_req_map, tvdb_req_map, title_req_map = await _get_request_user_mappings(db)

    seen_ids = set()
    filtered = []
    for m in raw_results:
        tmdb_id = m.get("id")
        if not tmdb_id or tmdb_id in seen_ids:
            continue

        title = m.get("title")
        release_date = m.get("release_date")
        overview = m.get("overview")
        poster_path = m.get("poster_path")

        # Only surface entries with a confirmed release date, real metadata to show,
        # and enough popularity to not be buried under obscure one-screen releases.
        if not title or not release_date or not overview or not poster_path:
            continue
        if m.get("popularity", 0) < UPCOMING_MOVIES_MIN_POPULARITY:
            continue

        seen_ids.add(tmdb_id)
        filtered.append(m)

    filtered = _limit_discover_results(filtered)
    filtered_ids = [m["id"] for m in filtered]
    # _get_release_info_batch doesn't touch the shared AsyncSession, so it's safe to
    # run concurrently with _get_cast_and_trailer (only one of the two uses `db`).
    cast_trailer_map, release_info_map = await asyncio.gather(
        _get_cast_and_trailer(client, filtered_ids, db, include_bios=False),
        _get_release_info_batch(client, filtered_ids)
    )

    # Cache metadata in DB & memory
    upcoming_keys = [f"movie:{m['title'].lower().strip()}" for m in filtered if m.get("title")]
    existing_meta = {}
    if upcoming_keys:
        existing_meta_res = await db.execute(select(MetadataCache).where(MetadataCache.cache_key.in_(upcoming_keys)))
        existing_meta = {row.cache_key: row for row in existing_meta_res.scalars().all()}

    db_added_any = False
    for m in filtered:
        tmdb_id = m["id"]
        title = m.get("title")
        title_clean = title.lower().strip()
        cache_key = f"movie:{title_clean}"
        cast_trailer = cast_trailer_map.get(tmdb_id, _EMPTY_CAST_TRAILER)
        cast_names = [c["name"] for c in cast_trailer.get("cast", [])]
        genres = [genre_map[gid] for gid in m.get("genre_ids", []) if gid in genre_map]
        poster_url = f"https://image.tmdb.org/t/p/w500{m.get('poster_path')}" if m.get('poster_path') else None
        backdrop_url = f"https://image.tmdb.org/t/p/original{m.get('backdrop_path')}" if m.get('backdrop_path') else None
        rating = round(m.get("vote_average", 0.0), 1) if m.get("vote_average") else None

        meta_dict = {
            "tmdb_id": tmdb_id,
            "title": title,
            "overview": m.get("overview"),
            "release_date": m.get("release_date"),
            "rating": rating,
            "vote_count": m.get("vote_count"),
            "genres": genres,
            "cast": cast_names,
            "runtime": None,
            "poster_path": poster_url,
            "backdrop_path": backdrop_url,
            "status": None,
            "source": "tmdb"
        }
        IN_MEMORY_METADATA_CACHE[cache_key] = meta_dict

        if cache_key not in existing_meta:
            new_entry = MetadataCache(
                cache_key=cache_key,
                title=title,
                media_type="movie",
                tmdb_id=tmdb_id,
                overview=m.get("overview"),
                release_date=m.get("release_date"),
                rating=rating,
                vote_count=m.get("vote_count"),
                genres_json=json.dumps(genres),
                cast_json=json.dumps(cast_names),
                runtime_minutes=None,
                poster_path=poster_url,
                backdrop_path=backdrop_url,
                status=None,
                source="tmdb"
            )
            db.add(new_entry)
            db_added_any = True

    if db_added_any:
        try:
            await db.commit()
        except Exception as e:
            logger.warning(f"Failed to save metadata cache for upcoming movies: {e}")
            await db.rollback()

    movies = []
    for m in filtered:
        tmdb_id = m["id"]
        title = m.get("title")
        title_clean = title.lower().strip()
        # Exact match only: substring containment (used by /search's local-title check)
        # produces massive false positives here against short local titles like "Up" or
        # "Ma" matching almost any longer upcoming title, which would wrongly hide
        # unowned movies behind an "Already In Library" badge.
        in_local_library = title_clean in local_titles
        in_radarr = tmdb_id in radarr_tmdb_ids

        if in_local_library:
            status = "in_library"
        elif in_radarr:
            status = "in_radarr"
        else:
            status = "not_added"

        cast_trailer = cast_trailer_map.get(tmdb_id, _EMPTY_CAST_TRAILER)

        # Determine watch providers / streaming services from TMDb release dates info
        releases = release_info_map.get(tmdb_id, [])
        
        # Determine the US release date of interest (prefer theatrical, and must be in/near the lookahead window)
        us_releases_in_window = []
        for r in releases:
            rtype = r.get("type")
            rdate_raw = r.get("release_date")
            if rtype in (2, 3, 4, 6) and rdate_raw:
                rdate = rdate_raw.split("T")[0]
                if start_date <= rdate <= end_date:
                    us_releases_in_window.append(r)
        
        selected_release = None
        if us_releases_in_window:
            # Sort: theatrical (2, 3) first, then by date
            us_releases_in_window.sort(key=lambda r: (0 if r.get("type") in (2, 3) else 1, r.get("release_date")))
            selected_release = us_releases_in_window[0]
        else:
            # Fallback 1: Earliest US release that is >= start_date
            future_releases = [
                r for r in releases 
                if r.get("type") in (2, 3, 4, 6) and r.get("release_date") and r.get("release_date").split("T")[0] >= start_date
            ]
            if future_releases:
                future_releases.sort(key=lambda r: (0 if r.get("type") in (2, 3) else 1, r.get("release_date")))
                selected_release = future_releases[0]
            else:
                # Fallback 2: Earliest US release overall
                all_us = [r for r in releases if r.get("type") in (2, 3, 4, 6) and r.get("release_date")]
                if all_us:
                    all_us.sort(key=lambda r: (0 if r.get("type") in (2, 3) else 1, r.get("release_date")))
                    selected_release = all_us[0]

        us_release_date = selected_release["release_date"].split("T")[0] if selected_release else None
        target_date_str = us_release_date or m.get("release_date")

        # Skip movies whose only reason for appearing in this window is a
        # digital/streaming (type 4/6) release when they already had a US
        # theatrical (type 2/3) release before this window started - that's a
        # movie moving to streaming after already opening in theaters (e.g. a
        # theatrical release from months ago now landing on Netflix), not a
        # new "coming soon" event. Applies unconditionally, including movies
        # already in the local library or Radarr - Coming Soon is for
        # discovering new releases, not a status tracker for existing adds.
        had_prior_theatrical_release = any(
            r.get("type") in (2, 3) and r.get("release_date") and r.get("release_date").split("T")[0] < start_date
            for r in releases
        )
        if had_prior_theatrical_release and selected_release and selected_release.get("type") in (4, 6):
            continue

        # Find if there is a digital or TV release in the US
        streaming_services = []
        is_streaming = False
        
        # 1. First scan for any release matching target date that is type 4 (Digital) or 6 (TV)
        for rel in releases:
            rdate_raw = rel.get("release_date")
            if not rdate_raw:
                continue
            rdate = rdate_raw.split("T")[0]
            rtype = rel.get("type")
            note = rel.get("note") or ""
            note_lower = note.lower()
            
            # If the release date matches or is very close, check its type
            if rdate == target_date_str:
                if rtype in (4, 6):
                    is_streaming = True
                    matched = _match_streaming_service(note_lower)

                    if matched:
                        streaming_services.extend(matched)
                    elif note:
                        # If there is a note, use it as a custom streaming location
                        streaming_services.append(note.strip())

        # 2. Fallback: if we didn't find a direct date match, but the movie has ANY type 4/6 release 
        # that has a known streaming service in its note, we can use it.
        if not is_streaming and not streaming_services:
            for rel in releases:
                rtype = rel.get("type")
                note = rel.get("note") or ""
                note_lower = note.lower()
                if rtype in (4, 6):
                    matched = _match_streaming_service(note_lower)

                    if matched:
                        streaming_services.extend(matched)
                        is_streaming = True
                        break

        # Remove duplicates
        streaming_services = list(dict.fromkeys(streaming_services))

        if is_streaming or streaming_services:
            release_medium = "Streaming"
            service_label = ", ".join(streaming_services) if streaming_services else "Digital Release"
            release_location = f"Streaming ({service_label})"
        else:
            release_medium = "Theater"
            release_location = "Theaters"

        movies.append({
            "tmdb_id": tmdb_id,
            "title": title,
            "release_date": target_date_str,
            "overview": m.get("overview"),
            "poster_url": f"https://image.tmdb.org/t/p/w500{m.get('poster_path')}",
            "backdrop_url": f"https://image.tmdb.org/t/p/original{m.get('backdrop_path')}" if m.get("backdrop_path") else None,
            "rating": round(m.get("vote_average", 0.0), 1) if m.get("vote_average") else None,
            "genres": [genre_map[gid] for gid in m.get("genre_ids", []) if gid in genre_map],
            "cast": cast_trailer["cast"],
            "trailer_url": cast_trailer["trailer_url"],
            "status": status,
            "requested_by": tmdb_req_map.get(tmdb_id) or title_req_map.get(title_clean),
            "release_medium": release_medium,
            "release_location": release_location
        })

    movies.sort(key=lambda x: (x["release_date"] or ""))

    return {
        "days": days,
        "start_date": start_date,
        "end_date": end_date,
        "count": len(movies),
        "movies": movies
    }

@router.post("/add-by-tmdb")
async def add_movie_by_tmdb_id(
    req: AddByTmdbRequest,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    Adds a movie to Radarr by TMDb ID (used by the Upcoming Movies view), looking up
    Radarr's own movie data for that TMDb ID so the correct titleSlug/images/etc. are used.
    """
    result = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "radarr"))
    config = result.scalar_one_or_none()
    if not config or not config.enabled:
        raise HTTPException(status_code=400, detail="Radarr is not configured.")

    client = RadarrClient(config.api_url, config.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
    movie_data = await client.lookup_movie_by_tmdb_id(req.tmdb_id)
    if not movie_data:
        raise HTTPException(status_code=404, detail="Could not find this movie via Radarr's lookup service.")

    res = await _add_movie_via_radarr(
        client, movie_data, req.monitored, req.search_now,
        quality_profile_id=req.quality_profile_id
    )
    if res.get("success") or "already in Radarr" in res.get("message", ""):
        title = movie_data.get("title", "Unknown Movie")
        tmdb_id = req.tmdb_id
        username = getattr(user, "username", None) or (user.get("username") if isinstance(user, dict) else "Unknown")
        msg = f"User '{username}' requested movie '{title}' (TMDb: {tmdb_id}) via Radarr"
        logger.info(f"[MEDIA_REQUEST] {msg}")
        db.add(AppLogEvent(
            level="INFO",
            event_tag="[MEDIA_REQUEST]",
            message=msg,
            context_json=json.dumps({"username": username, "tmdb_id": tmdb_id, "title": title})
        ))
        try:
            cache_key = f"movie:{title.lower().strip()}"
            meta_res = await db.execute(select(MetadataCache).where(MetadataCache.cache_key == cache_key))
            cached = meta_res.scalar_one_or_none()
            if cached:
                cached.requested_by = username
            else:
                db.add(MetadataCache(
                    cache_key=cache_key,
                    title=title,
                    media_type="movie",
                    requested_by=username,
                    source="none"
                ))
        except Exception as e:
            logger.warning(f"Failed to update MetadataCache requested_by: {e}")
        await db.commit()
        IN_MEMORY_RADARR_LIBRARY_CACHE.clear()
    return res

@router.get("/upcoming-tv")
async def get_upcoming_tv(
    days: int = Query(30, description="Lookahead window: 30, 60, 90, or 120 days"),
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD - overrides `days` with an explicit range (used by the Calendar view's month navigation)"),
    end_date: Optional[str] = Query(None, description="YYYY-MM-DD - paired with start_date"),
    db: AsyncSession = Depends(get_async_db)
):
    """
    Lists TV series with a first US air date (via TMDb's discover/tv endpoint) in
    the next 30/60/90/120 days - new series premieres you don't already have in
    Sonarr or your local library. This is a different, complementary view from
    Calendar, which shows episode-level air dates for shows you already track in
    Sonarr. TV has no theatrical-release concept, so unlike /upcoming-movies there's
    no theater/streaming distinction or prior-release exclusion logic here - current
    streaming providers are shown informationally via TMDb's watch/providers data.
    An explicit start_date/end_date pair (instead of days) lets the Calendar view
    request an arbitrary month directly rather than always being anchored to a
    "today + N days" lookahead window.
    """
    if days not in (30, 60, 90, 120):
        days = 30
    req_start_date, req_end_date = _resolve_upcoming_date_range(days, start_date, end_date)

    tmdb_res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "tmdb"))
    tmdb_cfg = tmdb_res.scalar_one_or_none()
    if not tmdb_cfg or not tmdb_cfg.enabled or not tmdb_cfg.api_key_encrypted:
        raise HTTPException(status_code=400, detail="TMDb is not configured. Add a TMDb API key in Admin settings first.")

    client = TMDbClient(tmdb_cfg.api_url, tmdb_cfg.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)

    discover_cache = await _get_cached_discover_tv_results(client, req_start_date, req_end_date)
    raw_results = discover_cache["raw_results"]
    genre_map = discover_cache["genre_map"]
    start_date = discover_cache["start_date"]
    end_date = discover_cache["end_date"]

    local_result = await db.execute(select(MediaItem.title).where(MediaItem.media_type == "tv"))
    local_titles = {t.lower().strip() for (t,) in local_result.all()}

    sonarr_tvdb_ids = await _get_sonarr_tvdb_ids(db)
    tmdb_req_map, tvdb_req_map, title_req_map = await _get_request_user_mappings(db)

    seen_ids = set()
    filtered = []
    for m in raw_results:
        tmdb_id = m.get("id")
        if not tmdb_id or tmdb_id in seen_ids:
            continue

        title = m.get("name")
        first_air_date = m.get("first_air_date")
        overview = m.get("overview")
        poster_path = m.get("poster_path")

        # Only surface entries with a confirmed air date, real metadata to show,
        # and enough popularity to not be buried under obscure/local-only titles.
        if not title or not first_air_date or not overview or not poster_path:
            continue
        if m.get("popularity", 0) < UPCOMING_MOVIES_MIN_POPULARITY:
            continue

        seen_ids.add(tmdb_id)
        filtered.append(m)

    filtered = _limit_discover_results(filtered)
    filtered_ids = [m["id"] for m in filtered]
    cast_trailer_map, external_ids_map, providers_map, networks_map = await asyncio.gather(
        _get_cast_and_trailer(client, filtered_ids, db, media_type="tv", include_bios=False),
        _get_tv_external_ids_batch(client, filtered_ids),
        _get_tv_watch_providers_batch(client, filtered_ids),
        _get_tv_networks_batch(client, filtered_ids),
    )

    # Cache metadata in DB & memory, same shape as movies (media_type="tv")
    upcoming_keys = [f"tv:{m['name'].lower().strip()}" for m in filtered if m.get("name")]
    existing_meta = {}
    if upcoming_keys:
        existing_meta_res = await db.execute(select(MetadataCache).where(MetadataCache.cache_key.in_(upcoming_keys)))
        existing_meta = {row.cache_key: row for row in existing_meta_res.scalars().all()}

    db_added_any = False
    for m in filtered:
        tmdb_id = m["id"]
        title = m.get("name")
        title_clean = title.lower().strip()
        cache_key = f"tv:{title_clean}"
        cast_trailer = cast_trailer_map.get(tmdb_id, _EMPTY_CAST_TRAILER)
        cast_names = [c["name"] for c in cast_trailer.get("cast", [])]
        genres = [genre_map[gid] for gid in m.get("genre_ids", []) if gid in genre_map]
        poster_url = f"https://image.tmdb.org/t/p/w500{m.get('poster_path')}" if m.get('poster_path') else None
        backdrop_url = f"https://image.tmdb.org/t/p/original{m.get('backdrop_path')}" if m.get('backdrop_path') else None
        rating = round(m.get("vote_average", 0.0), 1) if m.get("vote_average") else None

        if cache_key not in existing_meta:
            db.add(MetadataCache(
                cache_key=cache_key,
                title=title,
                media_type="tv",
                tmdb_id=tmdb_id,
                overview=m.get("overview"),
                release_date=m.get("first_air_date"),
                rating=rating,
                vote_count=m.get("vote_count"),
                genres_json=json.dumps(genres),
                cast_json=json.dumps(cast_names),
                runtime_minutes=None,
                poster_path=poster_url,
                backdrop_path=backdrop_url,
                status=None,
                source="tmdb"
            ))
            db_added_any = True

    if db_added_any:
        try:
            await db.commit()
        except Exception as e:
            logger.warning(f"Failed to save metadata cache for upcoming TV: {e}")
            await db.rollback()

    shows = []
    for m in filtered:
        tmdb_id = m["id"]
        title = m.get("name")
        title_clean = title.lower().strip()
        in_local_library = title_clean in local_titles
        tvdb_id = external_ids_map.get(tmdb_id)
        in_sonarr = bool(tvdb_id) and tvdb_id in sonarr_tvdb_ids

        if in_local_library:
            status = "in_library"
        elif in_sonarr:
            status = "in_sonarr"
        else:
            status = "not_added"

        cast_trailer = cast_trailer_map.get(tmdb_id, _EMPTY_CAST_TRAILER)
        providers = providers_map.get(tmdb_id, [])
        networks = networks_map.get(tmdb_id, [])
        requested_by = (tvdb_req_map.get(tvdb_id) if tvdb_id else None) or title_req_map.get(title_clean)

        shows.append({
            "tmdb_id": tmdb_id,
            "tvdb_id": tvdb_id,
            "title": title,
            "release_date": m.get("first_air_date"),
            "overview": m.get("overview"),
            "poster_url": f"https://image.tmdb.org/t/p/w500{m.get('poster_path')}",
            "backdrop_url": f"https://image.tmdb.org/t/p/original{m.get('backdrop_path')}" if m.get("backdrop_path") else None,
            "rating": round(m.get("vote_average", 0.0), 1) if m.get("vote_average") else None,
            "genres": [genre_map[gid] for gid in m.get("genre_ids", []) if gid in genre_map],
            "cast": cast_trailer["cast"],
            "trailer_url": cast_trailer["trailer_url"],
            "status": status,
            "requested_by": requested_by,
            "streaming_providers": providers,
            "networks": networks
        })

    shows.sort(key=lambda x: (x["release_date"] or ""))

    return {
        "days": days,
        "start_date": start_date,
        "end_date": end_date,
        "count": len(shows),
        "shows": shows
    }

@router.post("/add-tv-by-tmdb")
async def add_tv_show_by_tmdb_id(
    req: AddTvByTmdbRequest,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    Adds a TV series to Sonarr by TMDb ID (used by the Upcoming TV view). Sonarr is
    keyed by TVDB ID, so this looks up the show's tvdb_id via TMDb's external_ids
    first, then Sonarr's own lookup service for that tvdb_id.
    """
    result = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "sonarr"))
    sonarr_config = result.scalar_one_or_none()
    if not sonarr_config or not sonarr_config.enabled:
        raise HTTPException(status_code=400, detail="Sonarr is not configured.")

    tmdb_result = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "tmdb"))
    tmdb_config = tmdb_result.scalar_one_or_none()
    if not tmdb_config or not tmdb_config.enabled or not tmdb_config.api_key_encrypted:
        raise HTTPException(status_code=400, detail="TMDb is not configured.")

    tmdb_client = TMDbClient(tmdb_config.api_url, tmdb_config.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
    external_ids = await tmdb_client.get_tv_external_ids(req.tmdb_id)
    tvdb_id = external_ids.get("tvdb_id") if external_ids else None
    if not tvdb_id:
        raise HTTPException(status_code=404, detail="Could not find a TVDB ID for this show via TMDb.")

    sonarr_client = SonarrClient(sonarr_config.api_url, sonarr_config.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
    series_data = await sonarr_client.lookup_series_by_tvdb_id(tvdb_id)
    if not series_data:
        raise HTTPException(status_code=404, detail="Could not find this show via Sonarr's lookup service.")

    res = await _add_series_via_sonarr(
        sonarr_client, series_data, req.monitored, req.search_now,
        quality_profile_id=req.quality_profile_id,
        season_monitor=req.season_monitor
    )
    if res.get("success") or "already in Sonarr" in res.get("message", ""):
        title = series_data.get("title", "Unknown Show")
        username = getattr(user, "username", None) or (user.get("username") if isinstance(user, dict) else "Unknown")
        msg = f"User '{username}' requested TV show '{title}' (TVDb: {tvdb_id}) via Sonarr"
        logger.info(f"[MEDIA_REQUEST] {msg}")
        db.add(AppLogEvent(
            level="INFO",
            event_tag="[MEDIA_REQUEST]",
            message=msg,
            context_json=json.dumps({"username": username, "tvdb_id": tvdb_id, "title": title})
        ))
        try:
            cache_key = f"tv:{title.lower().strip()}"
            meta_res = await db.execute(select(MetadataCache).where(MetadataCache.cache_key == cache_key))
            cached = meta_res.scalar_one_or_none()
            if cached:
                cached.requested_by = username
            else:
                db.add(MetadataCache(
                    cache_key=cache_key,
                    title=title,
                    media_type="tv",
                    requested_by=username,
                    source="none"
                ))
        except Exception as e:
            logger.warning(f"Failed to update MetadataCache requested_by: {e}")
        await db.commit()
        IN_MEMORY_SONARR_LIBRARY_CACHE.clear()
    return res

@router.get("/syncthing/downloads")
async def get_syncthing_downloads(db: AsyncSession = Depends(get_async_db)):
    result = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "syncthing"))
    config = result.scalar_one_or_none()

    if not config or not config.enabled:
        return {"enabled": False, "folders": [], "warning": "Syncthing not configured."}

    client = SyncthingClient(config.api_url, config.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
    folders = await client.get_folders()
    
    folder_details = []
    total_need_bytes = 0

    async def fetch_folder_status(f):
        fid = f.get("id")
        label = f.get("label") or fid
        path = f.get("path")
        
        status_data = await client.get_db_status(fid)
        need_data = await client.get_db_need(fid)
        
        state = status_data.get("state", "unknown")
        global_bytes = status_data.get("globalBytes", 0)
        need_bytes = status_data.get("needBytes", 0)
        
        completion = 100.0
        if global_bytes > 0:
            completion = round(((global_bytes - need_bytes) / global_bytes) * 100, 1)
            if completion < 0:
                completion = 0.0
            if completion > 100:
                completion = 100.0
                
        # Parse needed files (in progress/queued)
        needed_files = []
        # progress section
        for item in need_data.get("progress", []):
            needed_files.append({
                "name": item.get("name"),
                "size": item.get("size", 0),
                "status": "downloading"
            })
        for item in need_data.get("queued", []):
            needed_files.append({
                "name": item.get("name"),
                "size": item.get("size", 0),
                "status": "queued"
            })
            
        return {
            "id": fid,
            "label": label,
            "path": path,
            "state": state,
            "completion": completion,
            "global_bytes": global_bytes,
            "need_bytes": need_bytes,
            "needed_files": needed_files
        }

    if folders:
        folder_details = await asyncio.gather(*[fetch_folder_status(f) for f in folders])
        for fd in folder_details:
            total_need_bytes += fd["need_bytes"]

    # Also fetch connections to get global metrics or total bytes
    connections_data = await client.get_connections()
    total_in_bytes = 0
    total_out_bytes = 0
    connections = connections_data.get("connections", {})
    for device_id, conn in connections.items():
        total_in_bytes += conn.get("inBytesTotal", 0)
        total_out_bytes += conn.get("outBytesTotal", 0)

    return {
        "enabled": True,
        "total_need_bytes": total_need_bytes,
        "total_in_bytes": total_in_bytes,
        "total_out_bytes": total_out_bytes,
        "folders": folder_details
    }

@router.get("/syncthing/history")
async def get_syncthing_history(
    days: int = Query(7, description="Number of days of history to fetch"),
    db: AsyncSession = Depends(get_async_db)
):
    result = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "syncthing"))
    config = result.scalar_one_or_none()

    if not config or not config.enabled:
        return {"enabled": False, "history": [], "warning": "Syncthing not configured."}

    client = SyncthingClient(config.api_url, config.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
    events = await client.get_events(events="ItemFinished", limit=1000)

    now = datetime.utcnow()
    cutoff = now - timedelta(days=days)
    
    history_items = []
    for ev in events:
        try:
            event_time = datetime.fromisoformat(ev.get("time").replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            continue
            
        if event_time < cutoff:
            continue
            
        data = ev.get("data", {})
        item_path = data.get("item")
        action = data.get("action", "update")
        
        if action == "delete":
            continue
            
        history_items.append({
            "id": ev.get("id"),
            "time": event_time.isoformat() + "Z",
            "folder": data.get("folder"),
            "filename": os.path.basename(item_path) if item_path else "Unknown",
            "path": item_path,
            "status": "Failed" if data.get("error") else "Completed",
            "error": data.get("error")
        })
        
    history_items.sort(key=lambda x: x["time"], reverse=True)
    
    return {
        "enabled": True,
        "days": days,
        "history": history_items
    }

@router.get("/sabnzbd/queue")
async def get_sabnzbd_queue(db: AsyncSession = Depends(get_async_db)):
    result = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "sabnzbd"))
    config = result.scalar_one_or_none()

    if not config or not config.enabled:
        return {"speed": "0 MB/s", "queue": [], "warning": "SABnzbd not configured."}

    client = SABnzbdClient(config.api_url, config.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
    queue = await client.get_queue()

    slots = queue.get("slots", [])
    return {
        "speed": queue.get("speed", "0 B/s"),
        "size_remaining": queue.get("sizeleft", "0 MB"),
        "queue": [
            {
                "nzo_id": s.get("nzo_id"),
                "filename": s.get("filename"),
                "size": s.get("size"),
                "percentage": s.get("percentage"),
                "timeleft": s.get("timeleft")
            }
            for s in slots
        ]
    }

def _parse_arr_date(date_str: Optional[str]) -> float:
    import re
    if not date_str:
        return 0.0
    try:
        # Python 3.10's fromisoformat only accepts 3 or 6 fractional digits,
        # but Sonarr/Radarr sometimes emit other widths (e.g. ".99791Z").
        normalized = re.sub(
            r'\.(\d+)',
            lambda m: '.' + m.group(1).ljust(6, '0')[:6],
            date_str.replace("Z", "+00:00")
        )
        return datetime.fromisoformat(normalized).timestamp()
    except Exception:
        return 0.0


def _normalize_arr_history(records: List[Dict[str, Any]], category: str) -> List[Dict[str, Any]]:
    """
    Converts Sonarr/Radarr history records into SABnzbd-history-shaped slots.
    Import events (downloadFolderImported) are the "completed" entries; Sonarr
    doesn't put size on that event (Radarr does), so fall back to the paired
    "grabbed" event's size, matched by downloadId.
    """
    grabbed_sizes: Dict[str, int] = {}
    for r in records:
        if r.get("eventType") == "grabbed":
            dl_id = r.get("downloadId")
            size = r.get("data", {}).get("size")
            if dl_id and size:
                grabbed_sizes[dl_id] = int(size)

    normalized = []
    for r in records:
        if r.get("eventType") != "downloadFolderImported":
            continue
        dl_id = r.get("downloadId")
        size_bytes = r.get("data", {}).get("size")
        size_bytes = int(size_bytes) if size_bytes else grabbed_sizes.get(dl_id, 0)
        normalized.append({
            "nzo_id": dl_id or f"{category}-history-{r.get('id')}",
            "name": r.get("sourceTitle"),
            "size": f"{round(size_bytes / (1024**3), 1)} GB" if size_bytes else "",
            "status": "Completed",
            "category": category,
            "completed": _parse_arr_date(r.get("date")),
            "bytes": size_bytes
        })
    return normalized


@router.get("/sabnzbd/history")
async def get_sabnzbd_history(db: AsyncSession = Depends(get_async_db)):
    import time

    async def _load_config(service_name: str) -> Optional[IntegrationConfig]:
        res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == service_name))
        return res.scalar_one_or_none()

    # These share the request-scoped AsyncSession, which SQLAlchemy does not allow to be
    # driven concurrently (asyncio.gather here previously raised IllegalStateChangeError /
    # "concurrent operations are not permitted" and leaked the underlying connection).
    sab_config = await _load_config("sabnzbd")
    sonarr_config = await _load_config("sonarr")
    radarr_config = await _load_config("radarr")

    async def _fetch_sab_slots() -> List[Dict[str, Any]]:
        if not sab_config or not sab_config.enabled:
            return []
        client = SABnzbdClient(sab_config.api_url, sab_config.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
        history = await client.get_history()
        return history.get("slots", [])

    async def _fetch_sonarr_slots() -> List[Dict[str, Any]]:
        if not sonarr_config or not sonarr_config.enabled:
            return []
        client = SonarrClient(sonarr_config.api_url, sonarr_config.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
        return _normalize_arr_history(await client.get_history(), "tv")

    async def _fetch_radarr_slots() -> List[Dict[str, Any]]:
        if not radarr_config or not radarr_config.enabled:
            return []
        client = RadarrClient(radarr_config.api_url, radarr_config.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
        return _normalize_arr_history(await client.get_history(), "movies")

    sab_slots, sonarr_slots, radarr_slots = await asyncio.gather(
        _fetch_sab_slots(), _fetch_sonarr_slots(), _fetch_radarr_slots()
    )

    if not sab_slots and not sonarr_slots and not radarr_slots:
        return {
            "slots": [],
            "stats": {
                "7d": {"count": 0, "size_gb": 0.0},
                "15d": {"count": 0, "size_gb": 0.0},
                "30d": {"count": 0, "size_gb": 0.0}
            }
        }

    now = time.time()

    stats = {
        "7d": {"count": 0, "size_bytes": 0},
        "15d": {"count": 0, "size_bytes": 0},
        "30d": {"count": 0, "size_bytes": 0}
    }

    processed_slots = []
    for s in sab_slots:
        processed_slots.append({
            "nzo_id": s.get("nzo_id"),
            "name": s.get("name"),
            "size": s.get("size"),
            "status": s.get("status"),
            "category": s.get("category"),
            "completed": float(s.get("completed") or 0),
            "bytes": int(s.get("bytes") or 0)
        })

    # SAB's own history is the richer source when it still has an entry (it hasn't
    # been cleaned up by Sonarr/Radarr's "remove completed downloads" yet) - only
    # add the Sonarr/Radarr version of a download if SAB no longer has it.
    known_nzo_ids = {s["nzo_id"] for s in processed_slots if s.get("nzo_id")}
    for s in sonarr_slots + radarr_slots:
        if s["nzo_id"] not in known_nzo_ids:
            processed_slots.append(s)
            known_nzo_ids.add(s["nzo_id"])

    processed_slots.sort(key=lambda s: s.get("completed") or 0, reverse=True)

    for s in processed_slots:
        completed_ts = s.get("completed") or 0
        bytes_val = s.get("bytes") or 0
        age_seconds = now - completed_ts

        if completed_ts > 0:
            if age_seconds <= 7 * 86400:
                stats["7d"]["count"] += 1
                stats["7d"]["size_bytes"] += bytes_val
            if age_seconds <= 15 * 86400:
                stats["15d"]["count"] += 1
                stats["15d"]["size_bytes"] += bytes_val
            if age_seconds <= 30 * 86400:
                stats["30d"]["count"] += 1
                stats["30d"]["size_bytes"] += bytes_val

    return {
        "slots": processed_slots,
        "stats": {
            "7d": {"count": stats["7d"]["count"], "size_gb": round(stats["7d"]["size_bytes"] / (1024**3), 2)},
            "15d": {"count": stats["15d"]["count"], "size_gb": round(stats["15d"]["size_bytes"] / (1024**3), 2)},
            "30d": {"count": stats["30d"]["count"], "size_gb": round(stats["30d"]["size_bytes"] / (1024**3), 2)}
        }
    }

async def _get_request_user_mappings(db: AsyncSession) -> Tuple[Dict[int, str], Dict[int, str], Dict[str, str]]:
    """
    Returns (tmdb_to_username, tvdb_to_username, title_to_username) mappings from AppLogEvent entries.
    Cached for REQUEST_USER_MAPPINGS_CACHE_TTL_SECONDS - this is called from nearly every
    discovery endpoint (search/upcoming/calendar/trending/media-detail/metadata), and the
    full [MEDIA_REQUEST] event history only grows, so re-scanning/re-parsing it on every
    request gets more expensive over the app's lifetime for no benefit.
    """
    now = time.time()
    cached = IN_MEMORY_REQUEST_USER_MAPPINGS_CACHE
    if cached and (now - cached.get("fetched_at", 0)) < REQUEST_USER_MAPPINGS_CACHE_TTL_SECONDS:
        return cached["tmdb_map"], cached["tvdb_map"], cached["title_map"]

    res = await db.execute(select(AppLogEvent).where(AppLogEvent.event_tag == "[MEDIA_REQUEST]"))
    events = res.scalars().all()
    
    tmdb_map = {}
    tvdb_map = {}
    title_map = {}
    for ev in events:
        uname = None
        title = None
        if ev.context_json:
            try:
                data = json.loads(ev.context_json)
                uname = data.get("username")
                if uname:
                    if "tmdb_id" in data:
                        tmdb_map[int(data["tmdb_id"])] = uname
                    if "tvdb_id" in data:
                        tvdb_map[int(data["tvdb_id"])] = uname
                    if "title" in data:
                        title_map[data["title"].lower().strip()] = uname
            except Exception:
                pass
        
        # Fallback to regex parsing of the logs for backwards compatibility
        if not uname and ev.message:
            import re
            m = re.search(r"User '([^']+)' requested (movie|TV show) '([^']+)'", ev.message)
            if m:
                uname = m.group(1)
                title = m.group(3)
                title_map[title.lower().strip()] = uname
                
                m_id = re.search(r"\(TMDb:\s*(\d+)\)", ev.message)
                if m_id:
                    tmdb_map[int(m_id.group(1))] = uname
                m_id_tv = re.search(r"\(TVDb:\s*(\d+)\)", ev.message)
                if m_id_tv:
                    tvdb_map[int(m_id_tv.group(1))] = uname

    IN_MEMORY_REQUEST_USER_MAPPINGS_CACHE.clear()
    IN_MEMORY_REQUEST_USER_MAPPINGS_CACHE.update({
        "fetched_at": now, "tmdb_map": tmdb_map, "tvdb_map": tvdb_map, "title_map": title_map
    })
    return tmdb_map, tvdb_map, title_map


async def _attach_missing_tv_tmdb_ids(client: TMDbClient, events: List[Dict[str, Any]]) -> None:
    """Fill tmdb_id on Calendar TV rows when metadata cache missed but Sonarr has tvdb_id."""
    needed = []
    for ev in events:
        if ev.get("type") != "tv" or ev.get("tmdb_id") or not ev.get("tvdb_id"):
            continue
        tvdb_id = ev["tvdb_id"]
        if tvdb_id in IN_MEMORY_TVDB_TO_TMDB_CACHE:
            ev["tmdb_id"] = IN_MEMORY_TVDB_TO_TMDB_CACHE[tvdb_id]
        else:
            needed.append(ev)
    if not needed:
        return
    unique_tvdb = list({ev["tvdb_id"] for ev in needed})

    async def one(tvdb_id):
        tid = await client.find_tv_tmdb_id_by_tvdb(tvdb_id)
        return tvdb_id, tid

    fetched = await asyncio.gather(*[one(v) for v in unique_tvdb])
    for tvdb_id, tid in fetched:
        IN_MEMORY_TVDB_TO_TMDB_CACHE[tvdb_id] = tid
    for ev in needed:
        ev["tmdb_id"] = IN_MEMORY_TVDB_TO_TMDB_CACHE.get(ev["tvdb_id"])


@router.get("/calendar")
async def get_combined_calendar(
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD - overrides the default -7/+23 day window (used by the Calendar view's month navigation)"),
    end_date: Optional[str] = Query(None, description="YYYY-MM-DD - paired with start_date"),
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(get_current_active_user)
):
    r_cfg_res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "radarr"))
    radarr_cfg = r_cfg_res.scalar_one_or_none()

    s_cfg_res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "sonarr"))
    sonarr_cfg = s_cfg_res.scalar_one_or_none()

    # An explicit start_date/end_date pair (the Calendar view's month grid, which
    # needs whatever month is currently navigated to) takes precedence over the
    # default -7/+23 day window (the List view's always-relevant-to-today range).
    resolved_range = _parse_explicit_date_range(start_date, end_date)
    if resolved_range:
        start_date, end_date = resolved_range
    else:
        today = datetime.utcnow()
        start_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        end_date = (today + timedelta(days=23)).strftime("%Y-%m-%d")

    events = []
    
    # Query request user mappings
    tmdb_req_map, tvdb_req_map, title_req_map = await _get_request_user_mappings(db)

    if radarr_cfg and radarr_cfg.enabled:
        try:
            rc = RadarrClient(radarr_cfg.api_url, radarr_cfg.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
            movies = await rc.get_calendar(start_date, end_date)
            for m in movies:
                air_date = m.get("physicalRelease") or m.get("digitalRelease") or m.get("inCinemas")
                if air_date:
                    events.append({
                        "type": "movie",
                        "title": m.get("title"),
                        "subtitle": f"Release Year: {m.get('year')}",
                        "date": air_date[:10],
                        "has_file": m.get("hasFile", False),
                        "monitored": m.get("monitored", False),
                        "overview": m.get("overview", ""),
                        "tmdb_id": m.get("tmdbId"),
                        "requested_by": tmdb_req_map.get(m.get("tmdbId")) or title_req_map.get(m.get("title", "").lower().strip())
                    })
        except Exception as e:
            logger.error(f"Error merging Radarr calendar: {e}")

    if sonarr_cfg and sonarr_cfg.enabled:
        try:
            sc = SonarrClient(sonarr_cfg.api_url, sonarr_cfg.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
            series_list = await sc.get_series()
            series_map = {s.get("id"): s.get("title") for s in series_list}
            series_tvdb_map = {s.get("id"): s.get("tvdbId") for s in series_list}
            series_network_map = {s.get("id"): s.get("network") for s in series_list}
            episodes = await sc.get_calendar(start_date, end_date)
            for ep in episodes:
                air_date = ep.get("airDate")
                series_id = ep.get("seriesId")
                series_title = series_map.get(series_id, "Unknown Show")
                tvdb_id = series_tvdb_map.get(series_id)
                if air_date:
                    se_num = f"S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d}"
                    events.append({
                        "type": "tv",
                        "title": series_title,
                        "subtitle": f"{se_num} — {ep.get('title', '')}",
                        "date": air_date,
                        "has_file": ep.get("hasFile", False),
                        "monitored": ep.get("monitored", False),
                        "overview": ep.get("overview", ""),
                        "network": series_network_map.get(series_id),
                        "tvdb_id": tvdb_id,
                        "requested_by": tvdb_req_map.get(tvdb_id) if tvdb_id is not None else title_req_map.get(series_title.lower().strip())
                    })
        except Exception as e:
            logger.error(f"Error merging Sonarr calendar: {e}")

    tmdb_res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "tmdb"))
    tmdb_cfg = tmdb_res.scalar_one_or_none()
    tmdb_client = None
    if tmdb_cfg and tmdb_cfg.enabled and tmdb_cfg.api_key_encrypted:
        tmdb_client = TMDbClient(tmdb_cfg.api_url, tmdb_cfg.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)

    if events:
        await _populate_media_details(events, db)
        if tmdb_client:
            await _attach_missing_tv_tmdb_ids(tmdb_client, events)

    events.sort(key=lambda x: x["date"])
    return events

@router.get("/download-queue")
async def get_download_queue(
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(get_current_active_user)
):
    """
    Merges Radarr's and Sonarr's own download queues (grabbed releases still
    downloading or pending import) into one list - distinct from the SABnzbd
    queue (/sabnzbd/queue), which reflects the raw NZB job rather than which
    movie/episode it belongs to or its import status.
    """
    r_cfg_res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "radarr"))
    radarr_cfg = r_cfg_res.scalar_one_or_none()

    s_cfg_res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "sonarr"))
    sonarr_cfg = s_cfg_res.scalar_one_or_none()

    def normalize(rec: Dict[str, Any], item_type: str, title: str) -> Dict[str, Any]:
        size = rec.get("size") or 0
        sizeleft = rec.get("sizeleft") or 0
        progress_pct = round((1 - (sizeleft / size)) * 100, 1) if size else 0
        return {
            "id": rec.get("id"),
            "type": item_type,
            "title": title,
            "status": rec.get("status"),
            "tracked_status": rec.get("trackedDownloadStatus"),
            "size_bytes": size,
            "sizeleft_bytes": sizeleft,
            "progress_pct": progress_pct,
            "timeleft": rec.get("timeleft"),
            "quality": (rec.get("quality") or {}).get("quality", {}).get("name"),
            "indexer": rec.get("indexer"),
            "download_client": rec.get("downloadClient"),
            "protocol": rec.get("protocol"),
            "error_message": rec.get("errorMessage"),
            "movie_id": rec.get("movieId") or (rec.get("movie") or {}).get("id"),
            "series_id": rec.get("seriesId") or (rec.get("series") or {}).get("id"),
            "episode_id": rec.get("episodeId") or (rec.get("episode") or {}).get("id"),
        }

    queue = []

    if radarr_cfg and radarr_cfg.enabled:
        try:
            rc = RadarrClient(radarr_cfg.api_url, radarr_cfg.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
            for rec in await rc.get_queue():
                movie_title = (rec.get("movie") or {}).get("title") or rec.get("title") or "Unknown Movie"
                queue.append(normalize(rec, "movie", movie_title))
        except Exception as e:
            logger.error(f"Error fetching Radarr queue: {e}")

    if sonarr_cfg and sonarr_cfg.enabled:
        try:
            sc = SonarrClient(sonarr_cfg.api_url, sonarr_cfg.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
            for rec in await sc.get_queue():
                series_title = (rec.get("series") or {}).get("title") or "Unknown Show"
                episode = rec.get("episode") or {}
                if episode.get("seasonNumber") is not None and episode.get("episodeNumber") is not None:
                    se_num = f"S{episode.get('seasonNumber', 0):02d}E{episode.get('episodeNumber', 0):02d}"
                    title = f"{series_title} - {se_num}"
                else:
                    title = series_title
                queue.append(normalize(rec, "tv", title))
        except Exception as e:
            logger.error(f"Error fetching Sonarr queue: {e}")

    return {"count": len(queue), "queue": queue}


class QueueActionRequest(BaseModel):
    action: str  # remove | retry | search
    type: str  # movie | tv
    id: int
    movie_id: Optional[int] = None
    series_id: Optional[int] = None
    episode_id: Optional[int] = None


@router.post("/download-queue/action")
async def act_on_download_queue(
    req: QueueActionRequest,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(get_admin_user)
):
    """Retry, remove, or search-now for a Radarr/Sonarr queue row (stuck grabs). Admin only:
    remove deletes the download from the client."""
    service = "radarr" if req.type == "movie" else "sonarr"
    result = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == service))
    config = result.scalar_one_or_none()
    if not config or not config.enabled:
        raise HTTPException(status_code=400, detail=f"{service.capitalize()} is not configured.")

    client = (
        RadarrClient(config.api_url, config.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
        if req.type == "movie"
        else SonarrClient(config.api_url, config.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
    )
    action = (req.action or "").lower()
    if action == "remove":
        ok = await client.delete_queue_item(req.id)
        if not ok:
            raise HTTPException(status_code=502, detail="Failed to remove queue item.")
        return {"success": True, "message": "Removed from queue."}
    if action == "retry":
        ok = await client.grab_queue_item(req.id)
        if not ok:
            raise HTTPException(status_code=502, detail="Failed to retry grab.")
        return {"success": True, "message": "Retry sent."}
    if action == "search":
        if req.type == "movie":
            movie_id = req.movie_id
            if not movie_id:
                raise HTTPException(status_code=400, detail="movie_id is required to search.")
            cmd = await client.trigger_command("MoviesSearch", movieIds=[movie_id])
        else:
            if req.episode_id:
                cmd = await client.trigger_command("EpisodeSearch", episodeIds=[req.episode_id])
            elif req.series_id:
                cmd = await client.trigger_command("SeriesSearch", seriesId=req.series_id)
            else:
                raise HTTPException(status_code=400, detail="series_id or episode_id is required to search.")
        if not (cmd.get("success") or cmd.get("id") or cmd.get("status") == "started"):
            raise HTTPException(status_code=502, detail=cmd.get("error") or "Search command failed.")
        return {"success": True, "message": "Search triggered."}
    raise HTTPException(status_code=400, detail="action must be remove, retry, or search.")


@router.get("/add-options")
async def get_add_options(
    type: str = Query("movie"),
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(get_current_active_user)
):
    """Quality profiles (and default id) for the add confirmation sheet."""
    service = "radarr" if type == "movie" else "sonarr"
    result = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == service))
    config = result.scalar_one_or_none()
    if not config or not config.enabled:
        return {"profiles": [], "default_profile_id": None}
    client = (
        RadarrClient(config.api_url, config.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
        if type == "movie"
        else SonarrClient(config.api_url, config.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
    )
    profiles = await client.get_profiles()
    slim = [{"id": p.get("id"), "name": p.get("name")} for p in profiles if p.get("id")]
    return {
        "profiles": slim,
        "default_profile_id": slim[0]["id"] if slim else None,
    }


@router.get("/watcher-status")
async def get_watcher_status(
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(get_current_active_user)
):
    """Whether media-manager-watcher.service has recently written a heartbeat to this DB."""
    from media_manager.heartbeat import STALE_AFTER_SECONDS, WATCHER_SERVICE
    res = await db.execute(select(ServiceHeartbeat).where(ServiceHeartbeat.service == WATCHER_SERVICE))
    row = res.scalar_one_or_none()
    if not row or not row.last_beat_at:
        return {
            "alive": False,
            "stale": True,
            "last_beat_at": None,
            "age_seconds": None,
            "pid": None,
            "hostname": None,
            "detail": "No heartbeat yet. The host watcher must POST to this portal (k3s uses /config/media.db; the watcher uses the checkout SQLite). Restart media-manager-watcher after this deploy.",
        }
    age = (datetime.utcnow() - row.last_beat_at).total_seconds()
    alive = age <= STALE_AFTER_SECONDS
    return {
        "alive": alive,
        "stale": not alive,
        "last_beat_at": row.last_beat_at.isoformat() + "Z",
        "age_seconds": int(age),
        "pid": row.pid,
        "hostname": row.hostname,
        "detail": None if alive else f"Last beat {int(age)}s ago (stale after {STALE_AFTER_SECONDS}s).",
    }


@router.get("/my-requests")
async def get_my_requests(
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    Lists the current user's own past media requests (from [MEDIA_REQUEST] log
    events) with a computed status: "available" (already downloaded/imported -
    checked against the local library and Radarr/Sonarr's own file-presence
    data), "downloading" (present in the Radarr/Sonarr download queue), or
    "requested" (neither yet - search still pending or hasn't matched).
    """
    username = getattr(user, "username", None) or (user.get("username") if isinstance(user, dict) else None)
    if not username:
        raise HTTPException(status_code=401, detail="Could not determine current user.")

    res = await db.execute(
        select(AppLogEvent).where(AppLogEvent.event_tag == "[MEDIA_REQUEST]").order_by(AppLogEvent.created_at.desc())
    )
    events = res.scalars().all()

    requests_list = []
    seen_keys = set()
    for ev in events:
        req_username = None
        tmdb_id = None
        tvdb_id = None
        title = None
        media_type = None

        if ev.context_json:
            try:
                data = json.loads(ev.context_json)
                req_username = data.get("username")
                if data.get("tmdb_id") is not None:
                    tmdb_id = int(data["tmdb_id"])
                if data.get("tvdb_id") is not None:
                    tvdb_id = int(data["tvdb_id"])
                title = data.get("title")
            except Exception:
                pass

        if not req_username and ev.message:
            import re
            m = re.search(r"User '([^']+)' requested (movie|TV show) '([^']+)'", ev.message)
            if m:
                req_username = m.group(1)
                media_type = "movie" if m.group(2) == "movie" else "tv"
                title = m.group(3)
                m_id = re.search(r"\(TMDb:\s*(\d+)\)", ev.message)
                if m_id:
                    tmdb_id = int(m_id.group(1))
                m_id_tv = re.search(r"\(TVDb:\s*(\d+)\)", ev.message)
                if m_id_tv:
                    tvdb_id = int(m_id_tv.group(1))

        if req_username != username or not title:
            continue

        if media_type is None:
            media_type = "tv" if tvdb_id else "movie"

        dedupe_key = ("tmdb", tmdb_id) if tmdb_id else (("tvdb", tvdb_id) if tvdb_id else ("title", title.lower().strip()))
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        # Events are already ordered newest-first, so the first occurrence of
        # a given dedupe_key is the most recent request for that title.
        requests_list.append({
            "title": title,
            "media_type": media_type,
            "tmdb_id": tmdb_id,
            "tvdb_id": tvdb_id,
            "requested_at": ev.created_at.isoformat(),
            "status": "requested"
        })

    if not requests_list:
        return {"count": 0, "requests": []}

    local_res = await db.execute(select(MediaItem.title, MediaItem.media_type))
    movie_titles = set()
    tv_titles = set()
    for t, mtype in local_res.all():
        (movie_titles if mtype == "movie" else tv_titles).add(t.lower().strip())

    tmdb_res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "tmdb"))
    tmdb_cfg = tmdb_res.scalar_one_or_none()

    radarr_has_file: Dict[int, bool] = {}
    radarr_queue_tmdb_ids = set()
    r_cfg_res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "radarr"))
    radarr_cfg = r_cfg_res.scalar_one_or_none()
    if radarr_cfg and radarr_cfg.enabled:
        try:
            rc = RadarrClient(radarr_cfg.api_url, radarr_cfg.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
            movies = await rc.get_movies()
            radarr_has_file = {m.get("tmdbId"): m.get("hasFile", False) for m in movies if m.get("tmdbId")}
            movie_id_to_tmdb = {m.get("id"): m.get("tmdbId") for m in movies if m.get("tmdbId")}
            for q in await rc.get_queue():
                tmdb_id_for_queue_item = movie_id_to_tmdb.get(q.get("movieId"))
                if tmdb_id_for_queue_item:
                    radarr_queue_tmdb_ids.add(tmdb_id_for_queue_item)
        except Exception as e:
            logger.error(f"Error checking Radarr status for my-requests: {e}")

    sonarr_has_file: Dict[int, bool] = {}
    sonarr_queue_tvdb_ids = set()
    s_cfg_res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "sonarr"))
    sonarr_cfg = s_cfg_res.scalar_one_or_none()
    if sonarr_cfg and sonarr_cfg.enabled:
        try:
            sc = SonarrClient(sonarr_cfg.api_url, sonarr_cfg.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
            series_list = await sc.get_series()
            sonarr_has_file = {
                s.get("tvdbId"): (s.get("statistics") or {}).get("episodeFileCount", 0) > 0
                for s in series_list if s.get("tvdbId")
            }
            series_id_to_tvdb = {s.get("id"): s.get("tvdbId") for s in series_list if s.get("tvdbId")}
            for q in await sc.get_queue():
                tvdb_id_for_queue_item = series_id_to_tvdb.get(q.get("seriesId"))
                if tvdb_id_for_queue_item:
                    sonarr_queue_tvdb_ids.add(tvdb_id_for_queue_item)
        except Exception as e:
            logger.error(f"Error checking Sonarr status for my-requests: {e}")

    for r in requests_list:
        title_clean = r["title"].lower().strip()
        if r["media_type"] == "movie":
            if title_clean in movie_titles or radarr_has_file.get(r["tmdb_id"], False):
                r["status"] = "available"
            elif r["tmdb_id"] in radarr_queue_tmdb_ids:
                r["status"] = "downloading"
        else:
            if title_clean in tv_titles or sonarr_has_file.get(r["tvdb_id"], False):
                r["status"] = "available"
            elif r["tvdb_id"] in sonarr_queue_tvdb_ids:
                r["status"] = "downloading"

    # Enrich with the same poster/overview/cast/trailer detail the other
    # discovery pages (Coming Soon, Trending) show inline, rather than a bare
    # title+status list. Degrades gracefully (blank fields) if TMDb isn't
    # configured or a title has no tmdb_id (e.g. parsed from an older log
    # event that predates tmdb_id tracking).
    for r in requests_list:
        r.update({"overview": None, "poster_url": None, "backdrop_url": None, "rating": None, "genres": [], "cast": [], "trailer_url": None, "networks": [], "streaming_providers": []})

    if tmdb_cfg and tmdb_cfg.enabled and tmdb_cfg.api_key_encrypted:
        client = TMDbClient(tmdb_cfg.api_url, tmdb_cfg.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
        movie_ids = [r["tmdb_id"] for r in requests_list if r["media_type"] == "movie" and r["tmdb_id"]]
        tv_ids = [r["tmdb_id"] for r in requests_list if r["media_type"] == "tv" and r["tmdb_id"]]

        async def _fetch_movie(tid):
            return tid, await client.get_movie_details(tid)

        async def _fetch_tv(tid):
            return tid, await client.get_tv_details(tid)

        detail_pairs = await asyncio.gather(
            *[_fetch_movie(tid) for tid in movie_ids],
            *[_fetch_tv(tid) for tid in tv_ids]
        )
        details_map = {tid: d for tid, d in detail_pairs if d}

        cast_trailer_map: Dict[int, Dict[str, Any]] = {}
        if movie_ids:
            cast_trailer_map.update(await _get_cast_and_trailer(client, movie_ids, db, media_type="movie"))
        if tv_ids:
            cast_trailer_map.update(await _get_cast_and_trailer(client, tv_ids, db, media_type="tv"))

        providers_map: Dict[int, List[str]] = {}
        if movie_ids:
            providers_map.update(await _get_movie_watch_providers_batch(client, movie_ids))
        if tv_ids:
            providers_map.update(await _get_tv_watch_providers_batch(client, tv_ids))

        for r in requests_list:
            tid = r["tmdb_id"]
            details = details_map.get(tid) if tid else None
            if details:
                r["overview"] = details.get("overview")
                r["poster_url"] = f"https://image.tmdb.org/t/p/w500{details.get('poster_path')}" if details.get("poster_path") else None
                r["backdrop_url"] = f"https://image.tmdb.org/t/p/original{details.get('backdrop_path')}" if details.get("backdrop_path") else None
                r["rating"] = round(details.get("vote_average", 0.0), 1) if details.get("vote_average") else None
                r["genres"] = [g.get("name") for g in details.get("genres", []) if g.get("name")]
                if r["media_type"] == "tv":
                    r["networks"] = [n.get("name") for n in details.get("networks", []) if n.get("name")]
            cast_trailer = cast_trailer_map.get(tid, _EMPTY_CAST_TRAILER) if tid else _EMPTY_CAST_TRAILER
            r["cast"] = cast_trailer["cast"]
            r["trailer_url"] = cast_trailer["trailer_url"]
            r["streaming_providers"] = providers_map.get(tid, []) if tid else []

    return {"count": len(requests_list), "requests": requests_list}

@router.get("/trending")
async def get_trending_media(
    media_type: str = Query("movie", description="movie or tv"),
    window: str = Query("week", description="day or week"),
    db: AsyncSession = Depends(get_async_db)
):
    """
    Lists currently-trending movies or TV shows via TMDb's /trending endpoint -
    popular right now, independent of release date. Distinct from Coming Soon,
    which is scoped to upcoming releases in a lookahead window.
    """
    if media_type not in ("movie", "tv"):
        media_type = "movie"
    if window not in ("day", "week"):
        window = "week"

    tmdb_res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "tmdb"))
    tmdb_cfg = tmdb_res.scalar_one_or_none()
    if not tmdb_cfg or not tmdb_cfg.enabled or not tmdb_cfg.api_key_encrypted:
        raise HTTPException(status_code=400, detail="TMDb is not configured. Add a TMDb API key in Admin settings first.")

    client = TMDbClient(tmdb_cfg.api_url, tmdb_cfg.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)

    cache_key = (media_type, window)
    cached = IN_MEMORY_TRENDING_CACHE.get(cache_key)
    now = time.time()
    if cached and (now - cached["fetched_at"]) < TRENDING_CACHE_TTL_SECONDS:
        raw_results = cached["raw_results"]
        genre_map = cached["genre_map"]
    else:
        try:
            raw_results = await asyncio.wait_for(client.get_trending(media_type, window), timeout=15.0)
            genre_map = await asyncio.wait_for(
                client.get_movie_genres() if media_type == "movie" else client.get_tv_genres(),
                timeout=10.0,
            )
            IN_MEMORY_TRENDING_CACHE[cache_key] = {
                "fetched_at": now, "raw_results": raw_results, "genre_map": genre_map
            }
        except asyncio.TimeoutError:
            logger.warning("TMDb trending list/genres timed out")
            raw_results = []
            genre_map = {}

    seen_ids = set()
    filtered = []
    for m in raw_results:
        tmdb_id = m.get("id")
        title = m.get("title") if media_type == "movie" else m.get("name")
        overview = m.get("overview")
        poster_path = m.get("poster_path")
        if not tmdb_id or tmdb_id in seen_ids or not title or not overview or not poster_path:
            continue
        seen_ids.add(tmdb_id)
        filtered.append(m)

    filtered = _limit_discover_results(filtered)
    filtered_ids = [m["id"] for m in filtered]
    cast_trailer_map: Dict[int, Dict[str, Any]] = {}
    providers_map: Dict[int, List[str]] = {}
    networks_map: Dict[int, List[str]] = {}
    external_ids_map: Dict[int, Optional[int]] = {}
    try:
        if media_type == "movie":
            cast_trailer_map, providers_map = await asyncio.wait_for(
                asyncio.gather(
                    _get_cast_and_trailer(client, filtered_ids, db, media_type=media_type, include_bios=False),
                    _get_movie_watch_providers_batch(client, filtered_ids),
                ),
                timeout=TRENDING_ENRICH_TIMEOUT_SECONDS,
            )
        else:
            cast_trailer_map, providers_map, networks_map, external_ids_map = await asyncio.wait_for(
                asyncio.gather(
                    _get_cast_and_trailer(client, filtered_ids, db, media_type=media_type, include_bios=False),
                    _get_tv_watch_providers_batch(client, filtered_ids),
                    _get_tv_networks_batch(client, filtered_ids),
                    _get_tv_external_ids_batch(client, filtered_ids),
                ),
                timeout=TRENDING_ENRICH_TIMEOUT_SECONDS,
            )
    except asyncio.TimeoutError:
        logger.warning(
            "Trending enrichment timed out for %s/%s (%s titles); returning titles without extra metadata",
            media_type, window, len(filtered_ids),
        )

    local_result = await db.execute(select(MediaItem.title).where(MediaItem.media_type == media_type))
    local_titles = {t.lower().strip() for (t,) in local_result.all()}

    radarr_tmdb_ids = set()
    sonarr_tvdb_ids = set()
    if media_type == "movie":
        radarr_tmdb_ids = await _get_radarr_tmdb_ids(db)
    else:
        sonarr_tvdb_ids = await _get_sonarr_tvdb_ids(db)

    tmdb_req_map, tvdb_req_map, title_req_map = await _get_request_user_mappings(db)

    items = []
    for m in filtered:
        tmdb_id = m["id"]
        title = m.get("title") if media_type == "movie" else m.get("name")
        title_clean = title.lower().strip()
        date_val = m.get("release_date") if media_type == "movie" else m.get("first_air_date")
        in_local_library = title_clean in local_titles

        if media_type == "movie":
            in_service = tmdb_id in radarr_tmdb_ids
            status = "in_library" if in_local_library else ("in_radarr" if in_service else "not_added")
            tvdb_id = None
            requested_by = tmdb_req_map.get(tmdb_id) or title_req_map.get(title_clean)
        else:
            tvdb_id = external_ids_map.get(tmdb_id)
            in_service = bool(tvdb_id) and tvdb_id in sonarr_tvdb_ids
            status = "in_library" if in_local_library else ("in_sonarr" if in_service else "not_added")
            requested_by = (tvdb_req_map.get(tvdb_id) if tvdb_id else None) or title_req_map.get(title_clean)

        cast_trailer = cast_trailer_map.get(tmdb_id, _EMPTY_CAST_TRAILER)

        items.append({
            "tmdb_id": tmdb_id,
            "tvdb_id": tvdb_id,
            "media_type": media_type,
            "title": title,
            "release_date": date_val,
            "overview": m.get("overview"),
            "poster_url": f"https://image.tmdb.org/t/p/w500{m.get('poster_path')}",
            "backdrop_url": f"https://image.tmdb.org/t/p/original{m.get('backdrop_path')}" if m.get("backdrop_path") else None,
            "rating": round(m.get("vote_average", 0.0), 1) if m.get("vote_average") else None,
            "genres": [genre_map[gid] for gid in m.get("genre_ids", []) if gid in genre_map],
            "cast": cast_trailer["cast"],
            "trailer_url": cast_trailer["trailer_url"],
            "status": status,
            "requested_by": requested_by,
            "streaming_providers": providers_map.get(tmdb_id, []),
            "networks": networks_map.get(tmdb_id, [])
        })

    return {
        "media_type": media_type,
        "window": window,
        "count": len(items),
        "items": items
    }

@router.get("/media-detail")
async def get_media_detail_full(
    tmdb_id: int = Query(...),
    type: str = Query("movie", description="movie or tv"),
    db: AsyncSession = Depends(get_async_db)
):
    """
    Full detail view for a single title (overview, cast, trailer, watch
    providers) plus a deduped Similar+Recommended "more like this" list, each
    with its own in-library/in-arr status - powers the reusable title detail
    modal used across Search, Coming Soon, Trending, and Calendar.
    """
    if type not in ("movie", "tv"):
        type = "movie"

    tmdb_res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "tmdb"))
    tmdb_cfg = tmdb_res.scalar_one_or_none()
    if not tmdb_cfg or not tmdb_cfg.enabled or not tmdb_cfg.api_key_encrypted:
        raise HTTPException(status_code=400, detail="TMDb is not configured. Add a TMDb API key in Admin settings first.")

    client = TMDbClient(tmdb_cfg.api_url, tmdb_cfg.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)

    details = await (client.get_movie_details(tmdb_id) if type == "movie" else client.get_tv_details(tmdb_id))
    if not details:
        raise HTTPException(status_code=404, detail="Could not find this title on TMDb.")

    title = details.get("title") if type == "movie" else details.get("name")
    if not title:
        raise HTTPException(status_code=404, detail="Could not find this title on TMDb.")

    if type == "movie":
        recs, similar, providers = await asyncio.gather(
            client.get_movie_recommendations(tmdb_id),
            client.get_similar_movies(tmdb_id),
            client.get_movie_watch_providers(tmdb_id)
        )
        runtime = details.get("runtime")
    else:
        recs, similar, providers = await asyncio.gather(
            client.get_tv_recommendations(tmdb_id),
            client.get_similar_tv(tmdb_id),
            client.get_tv_watch_providers(tmdb_id)
        )
        episode_runtimes = details.get("episode_run_time") or []
        runtime = episode_runtimes[0] if episode_runtimes else None

    cast_trailer_map = await _get_cast_and_trailer(client, [tmdb_id], db, media_type=type)
    cast_trailer = cast_trailer_map.get(tmdb_id, _EMPTY_CAST_TRAILER)

    # Recommendations first (generally closer matches), then similar, deduped
    # by id and excluding the title itself.
    seen_ids = {tmdb_id}
    related_raw = []
    for item in list(recs) + list(similar):
        rid = item.get("id")
        if not rid or rid in seen_ids or not item.get("poster_path"):
            continue
        seen_ids.add(rid)
        related_raw.append(item)
    related_raw = related_raw[:12]
    related_ids = [item["id"] for item in related_raw]

    items_res = await db.execute(select(MediaItem).where(MediaItem.media_type == type))
    all_local = list(items_res.scalars().all())
    title_clean = title.lower().strip()
    if type == "movie":
        local_titles = {it.title.lower().strip() for it in all_local}
        matches = [it for it in all_local if it.title.lower().strip() == title_clean]
    else:
        from media_manager.web.api.inventory import get_tv_show_name
        local_titles = set()
        matches = []
        for it in all_local:
            show = (get_tv_show_name(it.path) or it.title or "").lower().strip()
            if show:
                local_titles.add(show)
            if show == title_clean:
                matches.append(it)
    matches.sort(key=lambda it: it.quality_score or 0, reverse=True)

    # Loaded against the full `matches` set (not the [:20]-capped local_files
    # below) so a large show's total isn't undercounted.
    from media_manager.web.services.playback_lookup import (
        isoformat as _pb_isoformat, load_stats_by_media_item, merge_stats, sources_with_plays
    )
    stats_by_item = await load_stats_by_media_item(db, [it.id for it in matches])

    local_files = []
    for it in matches[:20]:
        it_stats = stats_by_item.get(it.id, [])
        it_play_count, it_last_played = merge_stats(it_stats)
        local_files.append({
            "id": it.id,
            "path": it.path,
            "resolution": it.resolution,
            "quality_score": it.quality_score,
            "size_bytes": it.size_bytes,
            "season": it.season,
            "episode": it.episode,
            "video_codec": it.video_codec,
            "audio_codec": it.audio_codec,
            "play_count": it_play_count,
            "last_played_at": _pb_isoformat(it_last_played),
            "play_sources": sources_with_plays(it_stats),
        })

    playback_summary: Dict[str, Any] = {}
    if type == "movie":
        primary_stats = stats_by_item.get(matches[0].id, []) if matches else []
        primary_count, primary_last_played = merge_stats(primary_stats)
        playback_summary = {
            "play_count": primary_count,
            "last_played_at": _pb_isoformat(primary_last_played),
            "play_sources": sources_with_plays(primary_stats),
        }
    else:
        total_plays = 0
        never_played = 0
        overall_last_played = None
        overall_sources = set()
        for it in matches:
            stats = stats_by_item.get(it.id, [])
            play_count, last_played_at = merge_stats(stats)
            total_plays += play_count
            if play_count == 0:
                never_played += 1
            if last_played_at and (not overall_last_played or last_played_at > overall_last_played):
                overall_last_played = last_played_at
            overall_sources.update(sources_with_plays(stats))
        playback_summary = {
            "total_plays": total_plays,
            "episode_count": len(matches),
            "never_played_episode_count": never_played,
            "last_played_at": _pb_isoformat(overall_last_played),
            "play_sources": sorted(overall_sources),
        }

    radarr_tmdb_ids = set()
    sonarr_tvdb_ids = set()
    tvdb_id = None
    related_external_ids: Dict[int, Optional[int]] = {}
    if type == "movie":
        radarr_tmdb_ids = await _get_radarr_tmdb_ids(db)
    else:
        sonarr_tvdb_ids = await _get_sonarr_tvdb_ids(db)
        external_ids = await client.get_tv_external_ids(tmdb_id)
        tvdb_id = external_ids.get("tvdb_id") if external_ids else None
        related_external_ids = await _get_tv_external_ids_batch(client, related_ids)

    def compute_status(item_id: int, item_title: str, item_tvdb_id: Optional[int]) -> str:
        title_clean = item_title.lower().strip()
        if title_clean in local_titles:
            return "in_library"
        if type == "movie":
            return "in_radarr" if item_id in radarr_tmdb_ids else "not_added"
        return "in_sonarr" if (item_tvdb_id and item_tvdb_id in sonarr_tvdb_ids) else "not_added"

    tmdb_req_map, tvdb_req_map, title_req_map = await _get_request_user_mappings(db)
    title_clean = title.lower().strip()
    requested_by = (
        tmdb_req_map.get(tmdb_id) if type == "movie" else (tvdb_req_map.get(tvdb_id) if tvdb_id else None)
    ) or title_req_map.get(title_clean)

    related = []
    for item in related_raw:
        rid = item["id"]
        rtitle = item.get("title") if type == "movie" else item.get("name")
        if not rtitle:
            continue
        r_tvdb_id = related_external_ids.get(rid) if type == "tv" else None
        related.append({
            "tmdb_id": rid,
            "tvdb_id": r_tvdb_id,
            "title": rtitle,
            "poster_url": f"https://image.tmdb.org/t/p/w342{item.get('poster_path')}",
            "rating": round(item.get("vote_average", 0.0), 1) if item.get("vote_average") else None,
            "status": compute_status(rid, rtitle, r_tvdb_id)
        })

    return {
        "tmdb_id": tmdb_id,
        "tvdb_id": tvdb_id,
        "type": type,
        "title": title,
        "release_date": details.get("release_date") if type == "movie" else details.get("first_air_date"),
        "overview": details.get("overview"),
        "poster_url": f"https://image.tmdb.org/t/p/w500{details.get('poster_path')}" if details.get("poster_path") else None,
        "backdrop_url": f"https://image.tmdb.org/t/p/original{details.get('backdrop_path')}" if details.get("backdrop_path") else None,
        "rating": round(details.get("vote_average", 0.0), 1) if details.get("vote_average") else None,
        "runtime": runtime,
        "genres": [g.get("name") for g in details.get("genres", []) if g.get("name")],
        "cast": cast_trailer["cast"],
        "trailer_url": cast_trailer["trailer_url"],
        "streaming_providers": providers,
        "networks": [n.get("name") for n in details.get("networks", []) if n.get("name")] if type == "tv" else [],
        "status": compute_status(tmdb_id, title, tvdb_id),
        "requested_by": requested_by,
        "related": related,
        "local_files": local_files,
        "playback": playback_summary,
    }

# Directory for cached resized image thumbnails
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "media_manager", "imgproxy")
os.makedirs(CACHE_DIR, exist_ok=True)
IN_MEMORY_THUMB_CACHE: Dict[str, bytes] = {}


def _read_file_bytes(path: str) -> Optional[bytes]:
    """Blocking file read - only ever call via asyncio.to_thread."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None


def _resize_and_cache_thumbnail(image_bytes: bytes, w: int, h: int, cache_path: str) -> bytes:
    """Blocking Pillow decode/resize/encode + disk-cache write - only ever call via asyncio.to_thread."""
    with Image.open(io.BytesIO(image_bytes)) as img:
        img.thumbnail((w, h))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=82, optimize=True)
        thumb_bytes = buf.getvalue()

    try:
        with open(cache_path, "wb") as f:
            f.write(thumb_bytes)
    except Exception:
        pass

    return thumb_bytes


ALLOWED_IMGPROXY_HOSTS = frozenset({
    "image.tmdb.org",
    "www.themoviedb.org",
    "assets.fanart.tv",
    "fanart.tv",
    "webservice.fanart.tv",
    "artworks.thetvdb.com",
})


def _allowed_media_roots():
    from media_manager.config import MEDIA_DIRECTORIES, SOURCE_DIRECTORIES
    roots = list(MEDIA_DIRECTORIES)
    for entry in SOURCE_DIRECTORIES or []:
        for key in ("from_dir", "to_dir"):
            if entry.get(key):
                roots.append(entry[key])
    return roots


def _is_allowed_local_image_path(path: str) -> bool:
    """Reject path traversal: file must exist under a configured media/source dir."""
    from pathlib import Path
    try:
        resolved = Path(path).resolve()
    except Exception:
        return False
    if not resolved.is_file():
        return False
    for root in _allowed_media_roots():
        try:
            resolved.relative_to(Path(root).resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def _is_allowed_remote_image_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return host in ALLOWED_IMGPROXY_HOSTS


@router.get("/imgproxy")
@router.head("/imgproxy")
async def image_proxy_cache(
    url: str = Query(..., description="Local path or remote URL of image"),
    w: int = Query(350, ge=50, le=2000),
    h: int = Query(500, ge=50, le=2000)
):
    """
    Lightweight, open-source Image Proxy & Thumbnail Caching Layer.
    Fetches remote URLs or reads local image paths, resizes them via Pillow,
    and caches compressed 18KB thumbnails in Memory & Disk for 0ms instant loading.
    """
    if not url:
        raise HTTPException(status_code=400, detail="URL parameter required")

    if url.startswith("http://") or url.startswith("https://"):
        if not _is_allowed_remote_image_url(url):
            raise HTTPException(status_code=400, detail="Image host is not allowed")
    else:
        target_file = url
        if url.startswith("/api/v1/integrations/serve-local-art?path="):
            target_file = unquote(url.split("path=", 1)[1])
        if not _is_allowed_local_image_path(target_file):
            raise HTTPException(status_code=404, detail="Image not found or failed to load")

    cache_key = hashlib.md5(f"{url}:{w}:{h}".encode("utf-8")).hexdigest()
    cache_path = os.path.join(CACHE_DIR, f"{cache_key}.jpg")

    # Tier 1: In-Memory Cache Check (0ms)
    if cache_key in IN_MEMORY_THUMB_CACHE:
        return Response(
            content=IN_MEMORY_THUMB_CACHE[cache_key],
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=31536000, immutable"}
        )

    # Tier 2: Persistent Disk Cache Check. File I/O is run in a worker thread so it
    # can't stall the single event loop the rest of the app's requests share.
    data = await asyncio.to_thread(_read_file_bytes, cache_path)
    if data is not None:
        IN_MEMORY_THUMB_CACHE[cache_key] = data
        return Response(
            content=data,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=31536000, immutable"}
        )

    # Tier 3: Uncached Image Fetch & Processing
    image_bytes = None
    if url.startswith("http://") or url.startswith("https://"):
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    image_bytes = resp.content
        except Exception as e:
            logging.warning(f"imgproxy remote fetch failed for {url}: {e}")
    else:
        # Local file path or /api/v1/integrations/serve-local-art path
        target_file = url
        if url.startswith("/api/v1/integrations/serve-local-art?path="):
            target_file = unquote(url.split("path=", 1)[1])

        image_bytes = await asyncio.to_thread(_read_file_bytes, target_file)

    if not image_bytes:
        raise HTTPException(status_code=404, detail="Image not found or failed to load")

    try:
        # Pillow's decode/resize/encode is CPU-bound and was previously running
        # directly on the event loop - a single large image (e.g. a 4K backdrop)
        # could block every other in-flight request for its whole duration. Run
        # it (and the resulting disk-cache write) in a worker thread instead.
        thumb_bytes = await asyncio.to_thread(_resize_and_cache_thumbnail, image_bytes, w, h, cache_path)

        IN_MEMORY_THUMB_CACHE[cache_key] = thumb_bytes

        return Response(
            content=thumb_bytes,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=31536000, immutable"}
        )
    except Exception as e:
        logging.error(f"imgproxy processing failed for {url}: {e}")
        raise HTTPException(status_code=500, detail="Failed to process image")

@router.get("/serve-local-art")
@router.head("/serve-local-art")
async def serve_local_art(path: str = Query(...)):
    """
    Serves a local image file safely from the filesystem with browser Cache-Control headers.
    """
    if not path or not os.path.exists(path) or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Image file not found")

    ext = os.path.splitext(path)[1].lower()
    if ext not in [".jpg", ".jpeg", ".png", ".webp", ".gif"]:
        raise HTTPException(status_code=400, detail="Invalid image extension")

    if not _is_allowed_local_image_path(path):
        raise HTTPException(status_code=404, detail="Image file not found")

    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})

def _build_title_candidates(title: str, path: Optional[str] = None) -> Tuple[List[str], Optional[int]]:
    """
    Builds a list of title candidates and extracts potential release year from file path or title.
    Handles dot variations (e.g. 'G I Jane' -> 'G.I. Jane'), folder names, and file stems.
    """
    import re
    from pathlib import Path

    candidates = []
    extracted_year = None

    # 1. Extract year
    year_match = re.search(r'\b(19\d{2}|20\d{2})\b', f"{title} {path or ''}")
    if year_match:
        extracted_year = int(year_match.group(1))

    # 2. Path folder & stem extraction
    if path:
        try:
            p = Path(path)
            folder_name = p.parent.name
            if folder_name and folder_name.lower() not in ["movies", "tvshows", "media", "mnt", "downloads", "download", "volume1"]:
                clean_folder = re.sub(r'\(?((?:19|20)\d{2})\)?', '', folder_name).strip(" -._()")
                if clean_folder and clean_folder not in candidates:
                    candidates.append(clean_folder)

            stem = p.stem
            clean_stem = re.sub(r'\(?((?:19|20)\d{2})\)?', '', stem).strip(" -._()")
            clean_stem = re.sub(r'\b(4K|2160p|1080p|720p|480p|HDTV|WEB-?DL|WEBRip|BluRay|REMUX|x264|x265|HEVC|AAC|DTS|AC3)\b', '', clean_stem, flags=re.I).strip(" -._()")
            if clean_stem and clean_stem not in candidates:
                candidates.append(clean_stem)
        except Exception:
            pass

    clean_title = title.strip() if title else ""
    if clean_title and clean_title not in candidates:
        candidates.append(clean_title)

    # 3. Dot insertion variations: "G I Jane" -> "G.I. Jane"
    dot_var1 = re.sub(r'\b([A-Za-z])\s+([A-Za-z])\b', r'\1.\2.', clean_title)
    if dot_var1 and dot_var1 not in candidates:
        candidates.append(dot_var1)

    dot_var2 = re.sub(r'\b([A-Za-z])\s+([A-Za-z])\b', r'\1\2', clean_title)
    if dot_var2 and dot_var2 not in candidates:
        candidates.append(dot_var2)

    # 4. Dot removal variation: "G.I. Jane" -> "G I Jane"
    space_var = re.sub(r'\.', ' ', clean_title)
    space_var = re.sub(r'\s+', ' ', space_var).strip()
    if space_var and space_var not in candidates:
        candidates.append(space_var)

    return candidates, extracted_year

@router.get("/artwork")
async def get_media_artwork(
    title: str = Query(..., min_length=1),
    type: str = Query("movie", description="'movie' or 'tv'"),
    tmdb_id: Optional[str] = Query(None),
    tvdb_id: Optional[str] = Query(None),
    path: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_async_db)
):
    """
    Fetches rich movie/TV artwork (posters, backdrops, clearlogos, banners) with strict cache-first pipeline:
    - Tier 1: Fast In-Memory cache (0ms instant response)
    - Tier 2: Persistent ArtworkCache database table
    - Tier 3: Local filesystem directory artwork scan (poster.jpg, backdrop.jpg, logo.png, etc.)
    - Tier 4: External API lookup (Fanart.tv / Arr fallback) -> Saves result to cache
    """
    clean_title = title.strip()
    media_type = type.lower() if type in ["movie", "tv"] else "movie"
    cache_key = f"{media_type}:{clean_title.lower()}"

    # Tier 1: Fast In-Memory Cache Check (0ms instant response)
    if cache_key in IN_MEMORY_ARTWORK_CACHE:
        return {
            "title": clean_title,
            "type": media_type,
            "artwork": IN_MEMORY_ARTWORK_CACHE[cache_key],
            "cached": True,
            "cache_tier": "memory"
        }

    # Tier 2: Persistent Database Cache Check
    db_cache_res = await db.execute(select(ArtworkCache).where(ArtworkCache.cache_key == cache_key))
    cached_db_item = db_cache_res.scalar_one_or_none()

    if cached_db_item:
        artwork_dict = {
            "poster": cached_db_item.poster_url,
            "background": cached_db_item.background_url,
            "logo": cached_db_item.logo_url,
            "banner": cached_db_item.banner_url,
            "source": cached_db_item.source
        }
        IN_MEMORY_ARTWORK_CACHE[cache_key] = artwork_dict
        return {
            "title": clean_title,
            "type": media_type,
            "artwork": artwork_dict,
            "cached": True,
            "cache_tier": "database"
        }

    # Tier 3: Local Filesystem Artwork Scan (Check local disk for poster.jpg, fanart.jpg, logo.png)
    local_query = select(MediaItem).where(
        or_(
            MediaItem.title.ilike(clean_title),
            MediaItem.path.ilike(f"%/{clean_title}/%")
        )
    ).limit(1)
    local_match = (await db.execute(local_query)).scalar_one_or_none()

    if local_match:
        local_art = find_local_artwork(local_match.path)
        if local_art["poster"] or local_art["background"] or local_art["logo"]:
            local_dict = {
                "poster": f"/api/v1/integrations/serve-local-art?path={quote(local_art['poster'])}" if local_art["poster"] else None,
                "background": f"/api/v1/integrations/serve-local-art?path={quote(local_art['background'])}" if local_art["background"] else None,
                "logo": f"/api/v1/integrations/serve-local-art?path={quote(local_art['logo'])}" if local_art["logo"] else None,
                "banner": f"/api/v1/integrations/serve-local-art?path={quote(local_art['banner'])}" if local_art["banner"] else None,
                "source": "local disk"
            }
            IN_MEMORY_ARTWORK_CACHE[cache_key] = local_dict
            try:
                new_cache_entry = ArtworkCache(
                    cache_key=cache_key,
                    title=clean_title,
                    media_type=media_type,
                    poster_url=local_dict["poster"],
                    background_url=local_dict["background"],
                    logo_url=local_dict["logo"],
                    banner_url=local_dict["banner"],
                    source="local disk"
                )
                db.add(new_cache_entry)
                await db.commit()
            except Exception as e:
                logger.warning(f"Failed to persist local-disk artwork cache entry for '{cache_key}': {e}")
                await db.rollback()
            return {
                "title": clean_title,
                "type": media_type,
                "artwork": local_dict,
                "cached": True,
                "cache_tier": "local_disk"
            }

    # Tier 4: External API Lookup (Fanart.tv / Arr fallback)
    fanart_res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "fanart"))
    fanart_cfg = fanart_res.scalar_one_or_none()

    artwork = {"poster": None, "background": None, "logo": None, "banner": None, "source": None}

    if fanart_cfg and fanart_cfg.enabled and fanart_cfg.api_key_encrypted:
        client = FanartClient(fanart_cfg.api_url, fanart_cfg.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
        target_id = tmdb_id if media_type == "movie" else tvdb_id

        if not target_id:
            arr_service = "radarr" if media_type == "movie" else "sonarr"
            arr_res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == arr_service))
            arr_cfg = arr_res.scalar_one_or_none()

            if arr_cfg and arr_cfg.enabled:
                if media_type == "movie":
                    rc = RadarrClient(arr_cfg.api_url, arr_cfg.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
                    matches = await rc.search_movies(clean_title)
                    if matches:
                        target_id = str(matches[0].get("tmdbId", ""))
                        for img in matches[0].get("images", []):
                            if img.get("coverType") == "poster" and img.get("remoteUrl"):
                                artwork["poster"] = img.get("remoteUrl")
                            elif img.get("coverType") == "fanart" and img.get("remoteUrl"):
                                artwork["background"] = img.get("remoteUrl")
                else:
                    sc = SonarrClient(arr_cfg.api_url, arr_cfg.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
                    matches = await sc.search_series(clean_title)
                    if matches:
                        target_id = str(matches[0].get("tvdbId", ""))
                        for img in matches[0].get("images", []):
                            if img.get("coverType") == "poster" and img.get("remoteUrl"):
                                artwork["poster"] = img.get("remoteUrl")
                            elif img.get("coverType") == "fanart" and img.get("remoteUrl"):
                                artwork["background"] = img.get("remoteUrl")

        if target_id:
            if media_type == "movie":
                raw_fanart = await client.get_movie_images(target_id)
            else:
                raw_fanart = await client.get_tv_images(target_id)

            extracted = client.extract_best_artwork(raw_fanart, media_type=media_type)
            if any(extracted.values()):
                if extracted.get("poster"): artwork["poster"] = extracted["poster"]
                if extracted.get("background"): artwork["background"] = extracted["background"]
                if extracted.get("logo"): artwork["logo"] = extracted["logo"]
                if extracted.get("banner"): artwork["banner"] = extracted["banner"]
                artwork["source"] = "fanart.tv"

    if not artwork["poster"] or not artwork["background"]:
        arr_service = "radarr" if media_type == "movie" else "sonarr"
        arr_res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == arr_service))
        arr_cfg = arr_res.scalar_one_or_none()

        if arr_cfg and arr_cfg.enabled:
            if media_type == "movie":
                rc = RadarrClient(arr_cfg.api_url, arr_cfg.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
                matches = await rc.search_movies(clean_title)
                if matches and matches[0].get("images"):
                    for img in matches[0]["images"]:
                        if not artwork["poster"] and img.get("coverType") == "poster" and img.get("remoteUrl"):
                            artwork["poster"] = img["remoteUrl"]
                        if not artwork["background"] and img.get("coverType") == "fanart" and img.get("remoteUrl"):
                            artwork["background"] = img["remoteUrl"]
                    artwork["source"] = artwork["source"] or "radarr"
            else:
                sc = SonarrClient(arr_cfg.api_url, arr_cfg.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
                matches = await sc.search_series(clean_title)
                if matches and matches[0].get("images"):
                    for img in matches[0]["images"]:
                        if not artwork["poster"] and img.get("coverType") == "poster" and img.get("remoteUrl"):
                            artwork["poster"] = img["remoteUrl"]
                        if not artwork["background"] and img.get("coverType") == "fanart" and img.get("remoteUrl"):
                            artwork["background"] = img["remoteUrl"]
                    artwork["source"] = artwork["source"] or "sonarr"

    # Populate In-Memory Cache
    IN_MEMORY_ARTWORK_CACHE[cache_key] = artwork

    # Persist in ArtworkCache database table
    try:
        new_cache_entry = ArtworkCache(
            cache_key=cache_key,
            title=clean_title,
            media_type=media_type,
            poster_url=artwork["poster"],
            background_url=artwork["background"],
            logo_url=artwork["logo"],
            banner_url=artwork["banner"],
            source=artwork["source"]
        )
        db.add(new_cache_entry)
        await db.commit()
    except Exception as e:
        logger.warning(f"Failed to persist artwork cache entry for '{cache_key}': {e}")
        await db.rollback()

    return {
        "title": clean_title,
        "type": media_type,
        "artwork": artwork,
        "cached": False,
        "cache_tier": "api"
    }

@router.get("/metadata")
async def get_media_metadata(
    title: str = Query(..., min_length=1),
    type: str = Query("movie", description="'movie' or 'tv'"),
    # Plain (non-Query) defaults here so this route function's real default value is
    # `None`, not a FastAPI Query sentinel object - _populate_media_details and tests
    # call this (and _get_media_metadata_impl) directly in Python, bypassing FastAPI's
    # dependency injection, and used to need an isinstance/hasattr hack to cope with that.
    year: Optional[int] = None,
    tmdb_id: Optional[int] = None,
    path: Optional[str] = None,
    db: AsyncSession = Depends(get_async_db)
):
    return await _get_media_metadata_impl(title, type, year, tmdb_id, path, db)

async def _get_media_metadata_impl(
    title: str,
    type: str,
    year: Optional[int],
    tmdb_id: Optional[int],
    path: Optional[str],
    db: AsyncSession
) -> Dict[str, Any]:
    """
    Metadata lookup pipeline adhering to:
    1. Check requests mappings
    2. Tier 1: Check In-Memory Cache (0ms response)
    3. Tier 2: Check Database Table (MetadataCache)
    4. Tier 3: Fetch via TMDb API -> Save to DB & Cache -> Return

    Plain internal function (not a FastAPI route) so it can be called directly from
    _populate_media_details with real Python values, not just from the /metadata route.
    """
    tmdb_req_map, tvdb_req_map, title_req_map = await _get_request_user_mappings(db)

    clean_title = title.strip()
    media_type = type.lower() if type in ["movie", "tv"] else "movie"
    cache_key = f"{media_type}:{clean_title.lower()}"

    # Tier 1: Check In-Memory Cache (skip if cached entry is empty/failed)
    if cache_key in IN_MEMORY_METADATA_CACHE:
        cached_mem = IN_MEMORY_METADATA_CACHE[cache_key]
        if cached_mem.get("tmdb_id") or cached_mem.get("rating") or cached_mem.get("overview"):
            tid = cached_mem.get("tmdb_id")
            cached_mem["requested_by"] = tmdb_req_map.get(tid) if tid else title_req_map.get(clean_title.lower())
            return {
                "title": clean_title,
                "type": media_type,
                "metadata": cached_mem,
                "cached": True,
                "cache_tier": "memory"
            }

    # Tier 2: Check Database Table (MetadataCache)
    db_res = await db.execute(select(MetadataCache).where(MetadataCache.cache_key == cache_key))
    cached_db_item = db_res.scalar_one_or_none()

    if cached_db_item and (cached_db_item.tmdb_id or cached_db_item.rating or cached_db_item.overview):
        meta_dict = {
            "tmdb_id": cached_db_item.tmdb_id,
            "title": cached_db_item.title,
            "overview": cached_db_item.overview,
            "release_date": cached_db_item.release_date,
            "rating": cached_db_item.rating,
            "vote_count": cached_db_item.vote_count,
            "genres": json.loads(cached_db_item.genres_json) if cached_db_item.genres_json else [],
            "cast": json.loads(cached_db_item.cast_json) if cached_db_item.cast_json else [],
            "trailer_url": None,
            "runtime": cached_db_item.runtime_minutes,
            "poster_path": cached_db_item.poster_path,
            "backdrop_path": cached_db_item.backdrop_path,
            "status": cached_db_item.status,
            "source": cached_db_item.source,
            "requested_by": cached_db_item.requested_by
        }
        # Check MovieCastCache for detailed cast and trailer URL
        if cached_db_item.tmdb_id:
            cast_res = await db.execute(select(MovieCastCache).where(MovieCastCache.tmdb_id == cached_db_item.tmdb_id))
            cast_row = cast_res.scalar_one_or_none()
            if cast_row:
                meta_dict["cast"] = json.loads(cast_row.cast_json) if cast_row.cast_json else []
                meta_dict["trailer_url"] = cast_row.trailer_url

        tid = cached_db_item.tmdb_id
        meta_dict["requested_by"] = tmdb_req_map.get(tid) if tid else title_req_map.get(clean_title.lower())
        IN_MEMORY_METADATA_CACHE[cache_key] = meta_dict
        return {
            "title": clean_title,
            "type": media_type,
            "metadata": meta_dict,
            "cached": True,
            "cache_tier": "database"
        }

    # Tier 3: Fetch via TMDb API using title candidates and year extraction
    candidates, extracted_year = _build_title_candidates(clean_title, path)
    query_year = year or extracted_year

    tmdb_res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "tmdb"))
    tmdb_cfg = tmdb_res.scalar_one_or_none()

    meta_dict = {
        "tmdb_id": None,
        "title": clean_title,
        "overview": None,
        "release_date": None,
        "rating": None,
        "vote_count": None,
        "genres": [],
        "cast": [],
        "runtime": None,
        "poster_path": None,
        "backdrop_path": None,
        "status": None,
        "source": "none"
    }

    if tmdb_cfg and tmdb_cfg.enabled and tmdb_cfg.api_key_encrypted:
        client = TMDbClient(tmdb_cfg.api_url, tmdb_cfg.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
        
        target_tmdb_id = tmdb_id
        if not target_tmdb_id:
            for cand in candidates:
                if media_type == "movie":
                    search_results = await client.search_movie(cand, year=query_year)
                    if not search_results and query_year:
                        search_results = await client.search_movie(cand, year=None)
                else:
                    search_results = await client.search_tv(cand, year=query_year)
                    if not search_results and query_year:
                        search_results = await client.search_tv(cand, year=None)
                
                if search_results:
                    target_tmdb_id = search_results[0].get("id")
                    break

        if target_tmdb_id:
            if media_type == "movie":
                details = await client.get_movie_details(target_tmdb_id)
            else:
                details = await client.get_tv_details(target_tmdb_id)

            if details:
                genres = [g.get("name") for g in details.get("genres", []) if g.get("name")]
                cast_trailer_res = await _get_cast_and_trailer(client, [target_tmdb_id], db, media_type=media_type)
                cast_trail = cast_trailer_res.get(target_tmdb_id)
                cast_details = cast_trail.get("cast") if cast_trail else []
                trailer_url = cast_trail.get("trailer_url") if cast_trail else None
                
                poster = f"https://image.tmdb.org/t/p/w500{details.get('poster_path')}" if details.get('poster_path') else None
                backdrop = f"https://image.tmdb.org/t/p/original{details.get('backdrop_path')}" if details.get('backdrop_path') else None

                meta_dict = {
                    "tmdb_id": details.get("id"),
                    "title": details.get("title") or details.get("name") or clean_title,
                    "overview": details.get("overview"),
                    "release_date": details.get("release_date") or details.get("first_air_date"),
                    "rating": round(details.get("vote_average", 0.0), 1) if details.get("vote_average") else None,
                    "vote_count": details.get("vote_count"),
                    "genres": genres,
                    "cast": cast_details,
                    "trailer_url": trailer_url,
                    "runtime": details.get("runtime") or (details.get("episode_run_time")[0] if details.get("episode_run_time") else None),
                    "poster_path": poster,
                    "backdrop_path": backdrop,
                    "status": details.get("status"),
                    "source": "tmdb"
                }

    # Save to In-Memory Cache
    tid = meta_dict.get("tmdb_id")
    meta_dict["requested_by"] = tmdb_req_map.get(tid) if tid else title_req_map.get(clean_title.lower())
    IN_MEMORY_METADATA_CACHE[cache_key] = meta_dict

    # Upsert to Database Table (overwrite previous empty entry if exists)
    try:
        cast_names = [c["name"] for c in meta_dict["cast"]] if isinstance(meta_dict["cast"], list) and meta_dict["cast"] and isinstance(meta_dict["cast"][0], dict) else meta_dict["cast"]
        if cached_db_item:
            cached_db_item.title = meta_dict["title"]
            cached_db_item.tmdb_id = meta_dict["tmdb_id"]
            cached_db_item.overview = meta_dict["overview"]
            cached_db_item.release_date = meta_dict["release_date"]
            cached_db_item.rating = meta_dict["rating"]
            cached_db_item.vote_count = meta_dict["vote_count"]
            cached_db_item.genres_json = json.dumps(meta_dict["genres"])
            cached_db_item.cast_json = json.dumps(cast_names)
            cached_db_item.runtime_minutes = meta_dict["runtime"]
            cached_db_item.poster_path = meta_dict["poster_path"]
            cached_db_item.backdrop_path = meta_dict["backdrop_path"]
            cached_db_item.status = meta_dict["status"]
            cached_db_item.source = meta_dict["source"]
            cached_db_item.requested_by = meta_dict.get("requested_by")
            cached_db_item.updated_at = datetime.utcnow()
        else:
            new_entry = MetadataCache(
                cache_key=cache_key,
                title=meta_dict["title"],
                media_type=media_type,
                tmdb_id=meta_dict["tmdb_id"],
                overview=meta_dict["overview"],
                release_date=meta_dict["release_date"],
                rating=meta_dict["rating"],
                vote_count=meta_dict["vote_count"],
                genres_json=json.dumps(meta_dict["genres"]),
                cast_json=json.dumps(cast_names),
                runtime_minutes=meta_dict["runtime"],
                poster_path=meta_dict["poster_path"],
                backdrop_path=meta_dict["backdrop_path"],
                status=meta_dict["status"],
                source=meta_dict["source"],
                requested_by=meta_dict.get("requested_by")
            )
            db.add(new_entry)
        await db.commit()
    except Exception as e:
        logger.warning(f"Failed to persist metadata cache entry for '{cache_key}': {e}")
        await db.rollback()

    return {
        "title": clean_title,
        "type": media_type,
        "metadata": meta_dict,
        "cached": False,
        "cache_tier": "tmdb_api"
    }

@router.get("/episode-metadata")
async def get_tv_episode_metadata(
    show_title: str = Query(..., min_length=1),
    season: int = Query(..., ge=0),
    episode: int = Query(..., ge=0),
    db: AsyncSession = Depends(get_async_db)
):
    """
    Fetches TV Episode metadata (episode title, overview, air_date, rating, still thumbnail) with multi-tier caching.
    """
    clean_show = show_title.strip()
    cache_key = f"episode:{clean_show.lower()}:s{season}:e{episode}"

    # Tier 1: Fast In-Memory Cache
    if cache_key in IN_MEMORY_METADATA_CACHE:
        return {
            "show_title": clean_show,
            "season": season,
            "episode": episode,
            "metadata": IN_MEMORY_METADATA_CACHE[cache_key],
            "cached": True,
            "cache_tier": "memory"
        }

    # Tier 2: Persistent DB Cache
    db_res = await db.execute(select(MetadataCache).where(MetadataCache.cache_key == cache_key))
    cached_db_item = db_res.scalar_one_or_none()
    if cached_db_item:
        meta_dict = {
            "title": cached_db_item.title,
            "overview": cached_db_item.overview,
            "release_date": cached_db_item.release_date,
            "rating": cached_db_item.rating,
            "poster_path": cached_db_item.poster_path,
            "source": cached_db_item.source
        }
        IN_MEMORY_METADATA_CACHE[cache_key] = meta_dict
        return {
            "show_title": clean_show,
            "season": season,
            "episode": episode,
            "metadata": meta_dict,
            "cached": True,
            "cache_tier": "database"
        }

    # Tier 3: Fetch via TMDb API
    tmdb_res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "tmdb"))
    tmdb_cfg = tmdb_res.scalar_one_or_none()

    meta_dict = {"title": None, "overview": None, "release_date": None, "rating": None, "poster_path": None, "source": "none"}

    if tmdb_cfg and tmdb_cfg.enabled and tmdb_cfg.api_key_encrypted:
        client = TMDbClient(tmdb_cfg.api_url, tmdb_cfg.api_key_encrypted, http_client=SHARED_HTTP_CLIENT)
        shows = await client.search_tv(clean_show)
        if shows:
            show_id = shows[0].get("id")
            ep_details = await client.get_tv_episode_details(show_id, season, episode)
            if ep_details:
                still_path = f"https://image.tmdb.org/t/p/w500{ep_details.get('still_path')}" if ep_details.get("still_path") else None
                meta_dict = {
                    "title": ep_details.get("name"),
                    "overview": ep_details.get("overview"),
                    "release_date": ep_details.get("air_date"),
                    "rating": round(ep_details.get("vote_average", 0.0), 1) if ep_details.get("vote_average") else None,
                    "poster_path": still_path,
                    "source": "tmdb"
                }

    IN_MEMORY_METADATA_CACHE[cache_key] = meta_dict
    try:
        new_entry = MetadataCache(
            cache_key=cache_key,
            title=meta_dict["title"] or clean_show,
            media_type="tv_episode",
            overview=meta_dict["overview"],
            release_date=meta_dict["release_date"],
            rating=meta_dict["rating"],
            poster_path=meta_dict["poster_path"],
            source=meta_dict["source"]
        )
        db.add(new_entry)
        await db.commit()
    except Exception as e:
        logger.warning(f"Failed to persist episode metadata cache entry for '{cache_key}': {e}")
        await db.rollback()

    return {
        "show_title": clean_show,
        "season": season,
        "episode": episode,
        "metadata": meta_dict,
        "cached": False,
        "cache_tier": "tmdb_api"
    }

class DirectoryCreate(BaseModel):
    path: str

@router.get("/directories")
async def list_monitored_directories(
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(get_current_active_user)
):
    import shutil
    def get_disk_space(path: str):
        try:
            total, used, free = shutil.disk_usage(path)
            free_pct = (free / total) * 100 if total > 0 else 0
            return {
                "total": total,
                "used": used,
                "free": free,
                "free_percent": round(free_pct, 1),
                "error": None
            }
        except Exception as e:
            return {
                "total": 0,
                "used": 0,
                "free": 0,
                "free_percent": 0,
                "error": str(e)
            }

    result = await db.execute(select(MonitoredDirectory))
    dirs = result.scalars().all()
    if not dirs:
        from media_manager.config import MEDIA_DIRECTORIES
        dirs = []
        for d_path in MEDIA_DIRECTORIES:
            new_dir = MonitoredDirectory(path=d_path)
            db.add(new_dir)
            dirs.append(new_dir)
        try:
            await db.commit()
            result = await db.execute(select(MonitoredDirectory))
            dirs = result.scalars().all()
        except Exception:
            await db.rollback()
            spaces = await asyncio.gather(*[asyncio.to_thread(get_disk_space, d) for d in MEDIA_DIRECTORIES])
            return [{"id": "default", "path": d, "disk_space": s} for d, s in zip(MEDIA_DIRECTORIES, spaces)]

    spaces = await asyncio.gather(*[asyncio.to_thread(get_disk_space, d.path) for d in dirs])
    return [{"id": d.id, "path": d.path, "disk_space": s} for d, s in zip(dirs, spaces)]

@router.post("/directories")
async def add_monitored_directory(
    payload: DirectoryCreate,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(get_admin_user)
):
    path = payload.path.strip()
    if not path:
        raise HTTPException(status_code=400, detail="Path cannot be empty.")
    
    result = await db.execute(select(MonitoredDirectory).where(MonitoredDirectory.path == path))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Directory is already monitored.")
    
    new_dir = MonitoredDirectory(path=path)
    db.add(new_dir)
    await db.commit()
    return {"success": True, "id": new_dir.id, "path": new_dir.path}

@router.delete("/directories/{dir_id}")
async def delete_monitored_directory(
    dir_id: str,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(get_admin_user)
):
    result = await db.execute(select(MonitoredDirectory).where(MonitoredDirectory.id == dir_id))
    directory = result.scalar_one_or_none()
    if not directory:
        # Check if dir_id is a default config path to allow deleting it by saving it to DB minus this path? 
        # Actually standard SQLite delete is fine.
        raise HTTPException(status_code=404, detail="Monitored directory not found.")
    
    await db.delete(directory)
    await db.commit()
    return {"success": True, "message": "Directory removed from monitor list."}

