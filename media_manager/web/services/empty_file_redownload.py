"""
Re-downloads media whose library file is a 0-byte placeholder.

Why a plain search isn't enough: the seedbox Radarr/Sonarr still list these
titles as downloaded (hasFile=True, size 0 - the mover truncates the seedbox
copy after copying it), so a MoviesSearch/EpisodeSearch grabs nothing. Each
item's stale file record is deleted first so the title counts as missing,
then a search is triggered. Movies Radarr doesn't know about at all are
looked up by title + year and added with search-on-add.

Once a download is requested, the local 0-byte placeholder and its DB row
are removed (re-checked as still 0 bytes on a healthy volume), so the dead
row can't be grouped as a "duplicate" of the incoming real file.
"""
import asyncio
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from guessit import guessit
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from media_manager.integrations.radarr import RadarrClient
from media_manager.scanner import guess_text
from media_manager.integrations.sonarr import SonarrClient
from media_manager.models import IntegrationConfig, MediaItem, PlaybackStat
from media_manager.quality import volume_is_healthy

logger = logging.getLogger(__name__)

_YEAR_IN_NAME = re.compile(r"^(?P<title>.+?)\s*\((?P<year>\d{4})\)")


def _norm(text: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


# ---------- pure matching helpers ----------

def parse_movie_title_year(path: str) -> Tuple[str, Optional[int]]:
    """'.../Bad Moms/Bad Moms (2016).mkv' -> ('Bad Moms', 2016); guessit fallback."""
    for name in (os.path.splitext(os.path.basename(path))[0], os.path.basename(os.path.dirname(path))):
        m = _YEAR_IN_NAME.match(name)
        if m:
            return m.group("title").strip(), int(m.group("year"))
    guess = guessit(os.path.basename(path))
    title = guess_text(guess, "title", os.path.splitext(os.path.basename(path))[0])
    year = guess.get("year")
    return title, (int(year) if year else None)


def match_movie(title: str, year: Optional[int], movies: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Radarr movie with the same normalized title (or folder name) and year."""
    wanted = _norm(title)
    for movie in movies:
        if year and movie.get("year") != year:
            continue
        folder = os.path.basename((movie.get("path") or "").rstrip("/"))
        folder_title = _YEAR_IN_NAME.match(folder).group("title") if _YEAR_IN_NAME.match(folder) else folder
        if wanted in (_norm(movie.get("title")), movie.get("cleanTitle") or "", _norm(folder_title)):
            return movie
    return None


def match_series(show_name: str, series_list: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Sonarr series whose title (or folder name) normalizes to the show folder name."""
    wanted = _norm(show_name)
    for series in series_list:
        folder = os.path.basename((series.get("path") or "").rstrip("/"))
        if wanted in (_norm(series.get("title")), series.get("cleanTitle") or "", _norm(folder)):
            return series
    return None


def match_episode(season: Optional[int], episode: Optional[int], episodes: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if season is None or episode is None:
        return None
    for ep in episodes:
        if ep.get("seasonNumber") == season and ep.get("episodeNumber") == episode:
            return ep
    return None


def pick_lookup_result(title: str, year: Optional[int], results: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Exact title+year hit from a Radarr /movie/lookup, never a fuzzy guess."""
    wanted = _norm(title)
    for result in results:
        if year and result.get("year") != year:
            continue
        if _norm(result.get("title")) == wanted and result.get("tmdbId"):
            return result
    return None


_PAREN_SUFFIX = re.compile(r"^(?P<name>.+?)\s*\((?P<tag>[^)]+)\)\s*$")


def pick_series_lookup(show_name: str, results: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Exact match from a Sonarr /series/lookup for a show folder name, never a
    fuzzy guess. 'Ghosts (US)' must match Sonarr's 'Ghosts (US)'; a year
    suffix like 'Doctor Who (2005)' may match title 'Doctor Who' + year 2005.
    """
    wanted = _norm(show_name)
    suffix = _PAREN_SUFFIX.match(show_name or "")
    year = int(suffix.group("tag")) if suffix and suffix.group("tag").isdigit() else None
    base = _norm(suffix.group("name")) if suffix else wanted
    for result in results:
        if not result.get("tvdbId"):
            continue
        title = _norm(result.get("title"))
        if title == wanted or (year and title == base and result.get("year") == year):
            return result
    return None


def _show_search_term(show_name: str) -> str:
    suffix = _PAREN_SUFFIX.match(show_name or "")
    return suffix.group("name") if suffix else show_name


def _show_name(path: str) -> str:
    from media_manager.web.api.inventory import get_tv_show_name
    return get_tv_show_name(path)


# ---------- DB / filesystem ----------

async def list_empty_items(db: AsyncSession, media_item_ids: Optional[List[str]] = None) -> List[MediaItem]:
    """Inventory rows recorded as 0 bytes (sample files are never indexed)."""
    query = select(MediaItem).where(MediaItem.size_bytes == 0)
    if media_item_ids is not None:
        query = query.where(MediaItem.id.in_(media_item_ids))
    res = await db.execute(query.order_by(MediaItem.media_type, MediaItem.path))
    return [item for item in res.scalars().all() if "sample" not in item.path.lower()]


def _disk_state(path: str) -> str:
    """'empty', 'missing', or 'has_data' (already replaced by a real file)."""
    try:
        return "empty" if os.path.getsize(path) == 0 else "has_data"
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "has_data"  # can't tell - never treat as safe to remove


async def _remove_placeholder(db: AsyncSession, item: MediaItem) -> bool:
    """Deletes the 0-byte file (if still 0 bytes) and the row. False if unsafe."""
    if not volume_is_healthy(item.storage_volume):
        return False
    state = _disk_state(item.path)
    if state == "has_data":
        return False
    if state == "empty":
        os.remove(item.path)
    await db.execute(delete(PlaybackStat).where(PlaybackStat.media_item_id == item.id))
    await db.execute(delete(MediaItem).where(MediaItem.id == item.id))
    return True


async def _client(db: AsyncSession, service: str, cls):
    res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == service))
    cfg = res.scalar_one_or_none()
    if not cfg or not cfg.enabled:
        return None
    from media_manager.web.api import integrations as integrations_module
    return cls(cfg.api_url, cfg.api_key_encrypted, http_client=integrations_module.SHARED_HTTP_CLIENT)


# ---------- planning + execution ----------

def _result(item: MediaItem, status: str, detail: str = "", **extra) -> Dict[str, Any]:
    return {
        "media_item_id": item.id, "path": item.path, "media_type": item.media_type,
        "title": item.title, "season": item.season, "episode": item.episode,
        "status": status, "detail": detail, **extra,
    }


async def _load_arr_state(db: AsyncSession, items: List[MediaItem]) -> Dict[str, Any]:
    """
    Clients + library lists. An empty list is ambiguous (down vs. genuinely
    empty), so it is followed by a status check: *_up says which it was.
    """
    state: Dict[str, Any] = {"radarr": None, "sonarr": None, "movies": [], "series": [],
                             "radarr_up": False, "sonarr_up": False}
    if any(i.media_type != "tv" for i in items):
        state["radarr"] = await _client(db, "radarr", RadarrClient)
        if state["radarr"]:
            state["movies"] = await state["radarr"].get_movies()
            state["radarr_up"] = bool(state["movies"]) or await state["radarr"].get_status()
    if any(i.media_type == "tv" for i in items):
        state["sonarr"] = await _client(db, "sonarr", SonarrClient)
        if state["sonarr"]:
            state["series"] = await state["sonarr"].get_series()
            state["sonarr_up"] = bool(state["series"]) or await state["sonarr"].get_status()
    return state


async def plan_redownload(db: AsyncSession, media_item_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Read-only preview: what would happen for each empty item. Planned actions:
    radarr_search (known movie), radarr_add (lookup + add), sonarr_search
    (known episode), sonarr_add (add the series, then that episode); anything
    else is a reason it can't be re-downloaded.
    """
    items = await list_empty_items(db, media_item_ids)
    state = await _load_arr_state(db, items)
    episodes_cache: Dict[int, List[Dict[str, Any]]] = {}
    planned = []
    for item in items:
        planned.append(await _plan_item(item, state, episodes_cache))
    return {"items": planned, "state": state, "episodes_cache": episodes_cache}


async def _plan_item(item: MediaItem, state: Dict[str, Any], episodes_cache: Dict[int, List]) -> Dict[str, Any]:
    if _disk_state(item.path) == "has_data":
        return _result(item, "no_longer_empty", "File on disk is no longer 0 bytes.")
    if item.media_type == "tv":
        if not state["sonarr"]:
            return _result(item, "unavailable", "Sonarr is not configured or enabled.")
        if not state["sonarr_up"]:
            return _result(item, "unavailable", "Sonarr is unreachable - check that it is running on the seedbox.")
        show = _show_name(item.path)
        series = match_series(show, state["series"])
        if not series:
            return _result(item, "sonarr_add", f"'{show}' is not in Sonarr - will look it up, add it (no other "
                                               f"episodes monitored) and request this episode.", lookup_show=show)
        if series["id"] not in episodes_cache:
            episodes_cache[series["id"]] = await state["sonarr"].get_episodes(series["id"])
        episode = match_episode(item.season, item.episode, episodes_cache[series["id"]])
        if not episode:
            return _result(item, "episode_not_found",
                           f"S{item.season}E{item.episode} not found in Sonarr series '{series.get('title')}'.")
        return _result(item, "sonarr_search", f"{series.get('title')} S{item.season:02d}E{item.episode:02d}",
                       series_id=series["id"], episode_id=episode["id"],
                       episode_file_id=episode.get("episodeFileId") if episode.get("hasFile") else None)

    if not state["radarr"]:
        return _result(item, "unavailable", "Radarr is not configured or enabled.")
    if not state["radarr_up"]:
        return _result(item, "unavailable", "Radarr is unreachable - check that it is running on the seedbox.")
    title, year = parse_movie_title_year(item.path)
    movie = match_movie(title, year, state["movies"])
    if movie:
        movie_file = movie.get("movieFile") or {}
        return _result(item, "radarr_search", f"{movie.get('title')} ({movie.get('year')})",
                       movie_id=movie["id"],
                       movie_file_id=movie_file.get("id") if movie.get("hasFile") else None)
    return _result(item, "radarr_add", f"{title} ({year}) is not in Radarr - will look it up and add it.",
                   lookup_title=title, lookup_year=year)


EPISODE_WAIT_ATTEMPTS = 10
EPISODE_WAIT_SECONDS = 3


async def _wait_for_episodes(sonarr: SonarrClient, series_id: int) -> List[Dict[str, Any]]:
    """A just-added series has no episodes until Sonarr's metadata refresh finishes."""
    for attempt in range(EPISODE_WAIT_ATTEMPTS):
        episodes = await sonarr.get_episodes(series_id)
        if episodes:
            return episodes
        if attempt < EPISODE_WAIT_ATTEMPTS - 1:
            await asyncio.sleep(EPISODE_WAIT_SECONDS)
    return []


async def _add_series_for(sonarr: SonarrClient, show: str, group: List[Dict[str, Any]],
                          episode_searches: List[Dict[str, Any]], results: List[Dict[str, Any]]) -> None:
    """
    Adds a missing series with *nothing* monitored and no search (so Sonarr
    doesn't grab the whole show), then queues just the empty episodes for the
    usual monitor + EpisodeSearch step.
    """
    from media_manager.web.api.integrations import _resolve_add_defaults, _resolve_language_profile_id

    match = pick_series_lookup(show, await sonarr.search_series(_show_search_term(show)))
    if not match:
        results.extend(dict(e, status="not_found", detail=f"No exact match for '{show}' in Sonarr's lookup.") for e in group)
        return
    try:
        profile_id, root = await _resolve_add_defaults(sonarr, "sonarr", None, None)
        language_id = await _resolve_language_profile_id(sonarr, None)
        added = await sonarr.add_series(match, profile_id, root, language_profile_id=language_id,
                                        monitored=True, search_now=False, season_monitor="none")
    except Exception as e:
        added = {"success": False, "error": getattr(e, "detail", None) or str(e)}
    series_id = (added.get("data") or {}).get("id") if added.get("success") else None
    if series_id is None and "already been added" in str(added.get("error")):
        existing = next((s for s in await sonarr.get_series() if s.get("tvdbId") == match.get("tvdbId")), None)
        series_id = existing.get("id") if existing else None
    if series_id is None:
        results.extend(dict(e, status="failed", detail=f"Sonarr add failed: {added.get('error')}") for e in group)
        return
    logger.info(f"[REDOWNLOAD] Added series '{match.get('title')}' to Sonarr (nothing monitored yet)")
    episodes = await _wait_for_episodes(sonarr, series_id)
    for entry in group:
        episode = match_episode(entry["season"], entry["episode"], episodes)
        if episode is None:
            results.append(dict(entry, status="added_pending",
                                detail=f"Added '{match.get('title')}' to Sonarr; its episode list isn't loaded yet - "
                                       f"run Re-download again in a minute."))
            continue
        episode_searches.append(dict(entry, series_id=series_id, episode_id=episode["id"], episode_file_id=None,
                                     detail=f"Added '{match.get('title')}' to Sonarr; "
                                            f"S{entry['season']:02d}E{entry['episode']:02d}"))


async def run_redownload(db: AsyncSession, media_item_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Executes plan_redownload's actions. Returns per-item results + counts."""
    from media_manager.web.api.integrations import _add_movie_via_radarr

    plan = await plan_redownload(db, media_item_ids)
    state = plan["state"]
    items_by_id = {i.id: i for i in await list_empty_items(db, [p["media_item_id"] for p in plan["items"]])}
    results: List[Dict[str, Any]] = []
    movie_searches: List[Dict[str, Any]] = []
    episode_searches: List[Dict[str, Any]] = []
    pending_series_adds: Dict[str, List[Dict[str, Any]]] = {}

    for entry in plan["items"]:
        if entry["status"] == "radarr_search":
            if entry["movie_file_id"] and not await state["radarr"].delete_movie_file(entry["movie_file_id"]):
                results.append(dict(entry, status="failed", detail="Radarr refused to delete the 0-byte file record."))
                continue
            movie_searches.append(entry)
        elif entry["status"] == "sonarr_search":
            if entry["episode_file_id"] and not await state["sonarr"].delete_episode_file(entry["episode_file_id"]):
                results.append(dict(entry, status="failed", detail="Sonarr refused to delete the 0-byte file record."))
                continue
            episode_searches.append(entry)
        elif entry["status"] == "sonarr_add":
            pending_series_adds.setdefault(entry["lookup_show"], []).append(entry)
        elif entry["status"] == "radarr_add":
            lookups = await state["radarr"].search_movies(f"{entry['lookup_title']} {entry['lookup_year'] or ''}".strip())
            match = pick_lookup_result(entry["lookup_title"], entry["lookup_year"], lookups)
            if not match:
                results.append(dict(entry, status="not_found", detail="No exact title/year match in Radarr's lookup."))
                continue
            try:
                added = await _add_movie_via_radarr(state["radarr"], match, monitored=True, search_now=True)
            except Exception as e:
                added = {"success": False, "error": getattr(e, "detail", None) or str(e)}
            if added.get("success"):
                results.append(dict(entry, status="requested", detail=f"Added '{match.get('title')}' to Radarr and searched."))
            else:
                results.append(dict(entry, status="failed", detail=f"Radarr add failed: {added.get('error')}"))
        else:
            results.append(entry)

    for show, group in pending_series_adds.items():
        await _add_series_for(state["sonarr"], show, group, episode_searches, results)

    if movie_searches:
        movie_ids = sorted({e["movie_id"] for e in movie_searches})
        # Deleting the file record can unmonitor the movie ("unmonitor deleted
        # movies"), and a search skips unmonitored movies - so re-monitor first.
        if not await state["radarr"].set_movies_monitored(movie_ids):
            logger.warning("[REDOWNLOAD] Could not re-monitor movies in Radarr; search may skip them.")
        cmd = await state["radarr"].trigger_command("MoviesSearch", movieIds=movie_ids)
        for e in movie_searches:
            results.append(dict(e, status="requested" if cmd.get("success") else "failed",
                                detail=e["detail"] if cmd.get("success") else f"MoviesSearch failed: {cmd.get('error')}"))
    if episode_searches:
        episode_ids = sorted({e["episode_id"] for e in episode_searches})
        if not await state["sonarr"].set_episodes_monitored(episode_ids):
            logger.warning("[REDOWNLOAD] Could not re-monitor episodes in Sonarr; search may skip them.")
        cmd = await state["sonarr"].trigger_command("EpisodeSearch", episodeIds=episode_ids)
        for e in episode_searches:
            results.append(dict(e, status="requested" if cmd.get("success") else "failed",
                                detail=e["detail"] if cmd.get("success") else f"EpisodeSearch failed: {cmd.get('error')}"))

    # No logging until after commit: the logger's DB handler inserts
    # app_log_events on its own connection and would wait out SQLite's busy
    # timeout behind this transaction's write lock.
    removal_errors = []
    for entry in results:
        if entry["status"] != "requested":
            continue
        item = items_by_id.get(entry["media_item_id"])
        try:
            entry["placeholder_removed"] = bool(item) and await _remove_placeholder(db, item)
        except OSError as e:
            entry["placeholder_removed"] = False
            removal_errors.append((entry["path"], e))
    await db.commit()

    for path, error in removal_errors:
        logger.warning(f"[REDOWNLOAD] Could not remove placeholder {path}: {error}")
    for entry in results:
        if entry["status"] == "requested":
            logger.info(f"[REDOWNLOAD] Requested re-download: {entry['detail']} ({entry['path']})")

    counts: Dict[str, int] = {}
    for entry in results:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    logger.info(f"[REDOWNLOAD] Finished: {counts}")
    return {"counts": counts, "results": results}
