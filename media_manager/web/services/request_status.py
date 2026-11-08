"""
Who requested what, and whether it has landed yet.

Shared by the My Requests page (`GET /integrations/my-requests`) and the
"your request is ready" email sweep (`request_notify`). Requests come from
`[MEDIA_REQUEST]` AppLogEvents; status is computed against the local library
and Radarr/Sonarr's own file-presence and queue data:
  - "available":   downloaded/imported
  - "downloading": in the Radarr/Sonarr download queue
  - "requested":   neither yet (search pending or not matched)
"""
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from media_manager.models import AppLogEvent, IntegrationConfig, MediaItem

logger = logging.getLogger(__name__)

_MSG_RE = re.compile(r"User '([^']+)' requested (movie|TV show) '([^']+)'")
_TMDB_RE = re.compile(r"\(TMDb:\s*(\d+)\)")
_TVDB_RE = re.compile(r"\(TVDb:\s*(\d+)\)")


def dedupe_key(tmdb_id: Optional[int], tvdb_id: Optional[int], title: str) -> str:
    """Stable string identity for one requested title (per user)."""
    if tmdb_id:
        return f"tmdb:{tmdb_id}"
    if tvdb_id:
        return f"tvdb:{tvdb_id}"
    return f"title:{title.lower().strip()}"


def _parse_event(ev) -> Optional[Dict[str, Any]]:
    req_username = tmdb_id = tvdb_id = title = media_type = None
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
        m = _MSG_RE.search(ev.message)
        if m:
            req_username = m.group(1)
            media_type = "movie" if m.group(2) == "movie" else "tv"
            title = m.group(3)
            m_id = _TMDB_RE.search(ev.message)
            if m_id:
                tmdb_id = int(m_id.group(1))
            m_id_tv = _TVDB_RE.search(ev.message)
            if m_id_tv:
                tvdb_id = int(m_id_tv.group(1))

    if not req_username or not title:
        return None
    if media_type is None:
        media_type = "tv" if tvdb_id else "movie"
    return {
        "username": req_username,
        "title": title,
        "media_type": media_type,
        "tmdb_id": tmdb_id,
        "tvdb_id": tvdb_id,
        "requested_at": ev.created_at,
        "status": "requested",
    }


def parse_request_events(events: Iterable, username: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Turn newest-first [MEDIA_REQUEST] events into one entry per (user, title),
    keeping the most recent request. `username` limits it to one user.
    """
    out: List[Dict[str, Any]] = []
    seen = set()
    for ev in events:
        r = _parse_event(ev)
        if not r or (username is not None and r["username"] != username):
            continue
        r["dedupe_key"] = dedupe_key(r["tmdb_id"], r["tvdb_id"], r["title"])
        key = (r["username"], r["dedupe_key"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


async def load_requests(db: AsyncSession, username: Optional[str] = None) -> List[Dict[str, Any]]:
    res = await db.execute(
        select(AppLogEvent).where(AppLogEvent.event_tag == "[MEDIA_REQUEST]").order_by(AppLogEvent.created_at.desc())
    )
    return parse_request_events(res.scalars().all(), username=username)


async def _enabled_config(db: AsyncSession, service: str) -> Optional[IntegrationConfig]:
    res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == service))
    cfg = res.scalar_one_or_none()
    return cfg if cfg and cfg.enabled else None


async def _radarr_state(db, radarr_cls, http_client) -> Tuple[Dict[int, bool], set]:
    has_file: Dict[int, bool] = {}
    queued = set()
    cfg = await _enabled_config(db, "radarr")
    if not cfg:
        return has_file, queued
    try:
        rc = radarr_cls(cfg.api_url, cfg.api_key_encrypted, http_client=http_client)
        movies = await rc.get_movies()
        has_file = {m.get("tmdbId"): m.get("hasFile", False) for m in movies if m.get("tmdbId")}
        id_to_tmdb = {m.get("id"): m.get("tmdbId") for m in movies if m.get("tmdbId")}
        for q in await rc.get_queue():
            tmdb_id = id_to_tmdb.get(q.get("movieId"))
            if tmdb_id:
                queued.add(tmdb_id)
    except Exception as e:
        logger.error(f"Error checking Radarr status for my-requests: {e}")
    return has_file, queued


async def _sonarr_state(db, sonarr_cls, http_client) -> Tuple[Dict[int, bool], set]:
    has_file: Dict[int, bool] = {}
    queued = set()
    cfg = await _enabled_config(db, "sonarr")
    if not cfg:
        return has_file, queued
    try:
        sc = sonarr_cls(cfg.api_url, cfg.api_key_encrypted, http_client=http_client)
        series_list = await sc.get_series()
        has_file = {
            s.get("tvdbId"): (s.get("statistics") or {}).get("episodeFileCount", 0) > 0
            for s in series_list if s.get("tvdbId")
        }
        id_to_tvdb = {s.get("id"): s.get("tvdbId") for s in series_list if s.get("tvdbId")}
        for q in await sc.get_queue():
            tvdb_id = id_to_tvdb.get(q.get("seriesId"))
            if tvdb_id:
                queued.add(tvdb_id)
    except Exception as e:
        logger.error(f"Error checking Sonarr status for my-requests: {e}")
    return has_file, queued


async def apply_statuses(db: AsyncSession, requests: List[Dict[str, Any]], radarr_cls, sonarr_cls,
                         http_client=None) -> List[Dict[str, Any]]:
    """
    Set each request's "status" in place. Client classes are passed in so the
    caller's (patchable) names are used.
    """
    if not requests:
        return requests
    local_res = await db.execute(select(MediaItem.title, MediaItem.media_type))
    movie_titles, tv_titles = set(), set()
    for t, mtype in local_res.all():
        (movie_titles if mtype == "movie" else tv_titles).add(t.lower().strip())

    radarr_has_file, radarr_queue = await _radarr_state(db, radarr_cls, http_client)
    sonarr_has_file, sonarr_queue = await _sonarr_state(db, sonarr_cls, http_client)

    for r in requests:
        title_clean = r["title"].lower().strip()
        if r["media_type"] == "movie":
            if title_clean in movie_titles or radarr_has_file.get(r["tmdb_id"], False):
                r["status"] = "available"
            elif r["tmdb_id"] in radarr_queue:
                r["status"] = "downloading"
        else:
            if title_clean in tv_titles or sonarr_has_file.get(r["tvdb_id"], False):
                r["status"] = "available"
            elif r["tvdb_id"] in sonarr_queue:
                r["status"] = "downloading"
    return requests
