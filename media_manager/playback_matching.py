"""
Pure helper functions for matching Plex/Emby/Jellyfin library items to this
app's MediaItem rows. Kept dependency-free (no DB/HTTP imports) so they're
independently unit-testable; media_manager/web/services/playback_sync.py is
the only caller.
"""
import json
import os
from typing import Any, Dict, List, Optional


def parse_path_mappings(extra_json: Optional[str]) -> List[Dict[str, str]]:
    """
    Parses the {"path_mappings": [{"source_prefix": ..., "local_prefix": ...}]}
    JSON blob stored in IntegrationConfig.extra_json for a playback source.
    Malformed/missing input yields an empty list rather than raising, since a
    misconfigured mapping should degrade to plain path matching, not break sync.
    """
    if not extra_json:
        return []
    try:
        data = json.loads(extra_json)
    except (TypeError, ValueError):
        return []

    mappings = data.get("path_mappings") if isinstance(data, dict) else None
    if not isinstance(mappings, list):
        return []

    result = []
    for entry in mappings:
        if isinstance(entry, dict) and entry.get("source_prefix") and entry.get("local_prefix"):
            result.append({
                "source_prefix": entry["source_prefix"],
                "local_prefix": entry["local_prefix"],
            })
    return result


def normalize_path(raw_path: str, mappings: List[Dict[str, str]]) -> str:
    """
    Rewrites a Plex/Emby/Jellyfin-reported file path onto this app's mount
    layout using the first matching configured prefix rule, e.g. a source
    container seeing /data/movies/X maps onto this app's /mnt/Movies/X.
    """
    if not raw_path:
        return raw_path
    for mapping in mappings:
        prefix = mapping["source_prefix"]
        if raw_path.startswith(prefix):
            return mapping["local_prefix"] + raw_path[len(prefix):]
    return raw_path


def to_remote_path(local_path: str, mappings: List[Dict[str, str]]) -> str:
    """The inverse of normalize_path: this app's path as the media server sees it."""
    if not local_path:
        return local_path
    for mapping in mappings:
        prefix = mapping["local_prefix"]
        if local_path.startswith(prefix):
            return mapping["source_prefix"] + local_path[len(prefix):]
    return local_path


def match_media_item(
    norm_path: str,
    items_by_path: Dict[str, Any],
    items_by_basename: Dict[str, List[Any]],
) -> Optional[Any]:
    """
    Exact normalized-path match first; falls back to a basename match only
    when it's unique, to avoid mismatching on ambiguous filenames (e.g. two
    different shows both having an "Episode 1.mkv").
    """
    if not norm_path:
        return None

    exact = items_by_path.get(os.path.normpath(norm_path))
    if exact is not None:
        return exact

    candidates = items_by_basename.get(os.path.basename(norm_path))
    if candidates and len(candidates) == 1:
        return candidates[0]
    return None
