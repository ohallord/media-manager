import logging
import os
import re
from typing import Dict, Iterable, List, Optional
from sqlalchemy import func
from media_manager.database import SyncSessionLocal
from media_manager.models import MediaItem, DuplicateGroup

logger = logging.getLogger(__name__)

_CAM_TOKENS = frozenset({"cam", "telesync", "ts", "scr", "screener"})


def _is_cam_or_telesync_release(name: str) -> bool:
    """True only for CAM/TS *release tags*, not random substrings like tmp…ts… files."""
    if "tele-sync" in name:
        return True
    tokens = [t for t in re.split(r"[^a-z0-9]+", name) if t]
    return any(t in _CAM_TOKENS for t in tokens)


_SEASON_DIR_RE = re.compile(r"^(season\s*\d+|specials)$", re.IGNORECASE)
_SONARR_LOOSE_RE = re.compile(r"^(.+?)\s+-\s+\d{1,2}x\d{1,3}\b")


def tv_series_name(path: str, title: str) -> str:
    """Series name for a TV file, for duplicate grouping.

    MediaItem.title for TV is whatever guessit found in the *filename*: the
    episode title for files inside ``Show/Season N/`` folders, the show name
    for loose ``Show - 4x02 - ...`` files. Grouping on it split one episode's
    copies apart and merged different shows that share an episode title
    ("Episode 2" S1E2), so group on the show instead.
    """
    parent = os.path.dirname(path)
    if _SEASON_DIR_RE.match(os.path.basename(parent)):
        show = os.path.basename(os.path.dirname(parent))
        if show:
            return show
    m = _SONARR_LOOSE_RE.match(os.path.basename(path))
    if m:
        return m.group(1).strip()
    return title


def _series_key(name: str) -> str:
    """Case- and punctuation-insensitive key; years and country tags are kept
    so "Hostage (2023)" and "Hostage (2025)" never merge."""
    return " ".join(re.split(r"[^0-9a-z]+", name.lower())).strip()


def volume_is_healthy(volume: str) -> bool:
    """True when the volume path is a non-empty directory (mount looks present)."""
    try:
        return bool(volume) and os.path.isdir(volume) and len(os.listdir(volume)) > 0
    except OSError:
        return False


def parse_path_qualities(path: str) -> dict:
    name = path.lower()
    
    # Source / Release Type Detection
    source = "web-dl"  # default fallback
    if "remux" in name:
        source = "remux"
    elif "bluray" in name or "bdrip" in name or "brrip" in name:
        source = "bluray"
    elif "web-dl" in name or "webdl" in name or "amazon" in name or "netflix" in name or "hmax" in name or "dnp" in name:
        source = "web-dl"
    elif "webrip" in name or "web-rip" in name:
        source = "webrip"
    elif "hdtv" in name:
        source = "hdtv"
    elif "dvd" in name or "dvdrip" in name:
        source = "dvd"
    elif _is_cam_or_telesync_release(name):
        source = "cam"
        
    # HDR / Color Space. "dv" is a release tag, not a substring: "dvdrip" and
    # temp dirs like mm_proc_file_dv12 must stay SDR.
    tokens = {t for t in re.split(r"[^a-z0-9+]+", name) if t}
    has_dv = bool(tokens & {"dv", "dovi", "dolbyvision"}) or "dolby vision" in name
    has_hdr10 = bool(tokens & {"hdr10", "hdr10+", "hdr10plus"})
    hdr = "sdr"
    if has_dv and has_hdr10:
        hdr = "dv_hdr10"
    elif has_dv:
        hdr = "dv"
    elif tokens & {"hdr10+", "hdr10plus"}:
        hdr = "hdr10plus"
    elif has_hdr10 or "hdr" in tokens:
        hdr = "hdr10"
        
    # Repack / Proper
    repack = "repack" in name or "proper" in name
        
    return {"source": source, "hdr": hdr, "repack": repack}

# Bits a codec needs for the same picture quality, relative to h264 (1.0).
# Feeds the bitrate tie-breaker so an HEVC file at half its h264 source's
# bitrate isn't scored as the "worse" copy (which would make the mover reject
# a transcode, or delete-worse-duplicates remove it).
CODEC_EFFICIENCY = {
    "hevc": 2.0,
    "av1": 2.0,
    "vp9": 1.6,
    "h264": 1.0,
    "vc1": 0.9,
    "mpeg4": 0.7,
    "msmpeg4v3": 0.7,
    "msmpeg4v2": 0.7,
    "msmpeg4v1": 0.7,
    "mpeg2video": 0.6,
}


def effective_bitrate(bitrate: int, video_codec: Optional[str] = None, video_bitrate: Optional[int] = None) -> float:
    """
    h264-equivalent bitrate. Only the video stream is scaled when its bitrate
    is known (audio bits are codec-independent); otherwise the container
    total is. Unknown codecs count as h264.
    """
    bitrate = bitrate or 0
    mult = CODEC_EFFICIENCY.get((video_codec or "").lower(), 1.0)
    if video_bitrate and 0 < video_bitrate <= bitrate:
        return (bitrate - video_bitrate) + video_bitrate * mult
    return bitrate * mult


def calculate_quality_score(resolution: str, height: int, audio_codec: str, audio_channels: int, bitrate: int, path: str = "",
                            video_codec: Optional[str] = None, video_bitrate: Optional[int] = None) -> int:
    """
    Calculates numerical quality score based on TRaSH Guides:
    - Base Resolution: 4K (1000), 1080p (500), 720p (200), SD (50)
    - Source: Remux (+500), Bluray (+400), WEB-DL (+300), WEBRip (+200), HDTV (+100), DVD (+50), CAM/TS (-1000)
    - HDR/Color: Dolby Vision + HDR10+ (+300), Dolby Vision (+250), HDR10+ (+200), HDR10 (+150), SDR (0)
    - Audio: Atmos (+300), TrueHD/DTS-X (+250), DTS-HD (+200), DTS (+150), DD+/EAC3 (+100), DD/AC3 (+50), AAC (+20)
    - Repack/Proper: +50
    - Bitrate Tie-breaker: (h264-equivalent bitrate_mbps * 5), see effective_bitrate()
    """
    # Base Resolution
    res_score = 50
    if height >= 2160 or "2160p" in resolution or "4k" in resolution.lower():
        res_score = 1000
    elif height >= 1080 or "1080p" in resolution:
        res_score = 500
    elif height >= 720 or "720p" in resolution:
        res_score = 200
        
    # Parse path attributes
    attrs = parse_path_qualities(path)
    
    # Source scores (TRaSH Guides style)
    source_scores = {
        "remux": 500,
        "bluray": 400,
        "web-dl": 300,
        "webrip": 200,
        "hdtv": 100,
        "dvd": 50,
        "cam": -1000
    }
    src_score = source_scores.get(attrs["source"], 0)
    
    # Color Space / HDR
    hdr_scores = {
        "dv_hdr10": 300,
        "dv": -1000, # Dolby Vision without HDR10 fallback (DoVI Profile 5) is widely incompatible, penalize it
        "hdr10plus": 200,
        "hdr10": 150,
        "sdr": 0
    }
    hdr_score = hdr_scores.get(attrs["hdr"], 0)
    
    # Audio codec
    acodec_clean = str(audio_codec).lower()
    audio_score = 0
    if "atmos" in acodec_clean:
        audio_score = 300
    elif "truehd" in acodec_clean or "dts-x" in acodec_clean or "dtsx" in acodec_clean:
        audio_score = 250
    elif "dts-hd" in acodec_clean or "dtshd" in acodec_clean:
        audio_score = 200
    elif "dts" in acodec_clean:
        audio_score = 150
    elif "eac3" in acodec_clean or "dd+" in acodec_clean:
        audio_score = 100
    elif "ac3" in acodec_clean or "dd" in acodec_clean:
        audio_score = 50
    elif "aac" in acodec_clean:
        audio_score = 20
        
    # Repack/Proper
    repack_score = 50 if attrs["repack"] else 0
    
    # Bitrate (Mbps) and size bonuses
    eff_bitrate = effective_bitrate(bitrate, video_codec, video_bitrate)
    bitrate_mbps = float(eff_bitrate) / 1000000.0 if eff_bitrate > 0 else 0.0
    bitrate_bonus = min(int(bitrate_mbps * 5), 150)
    
    total_score = res_score + src_score + hdr_score + audio_score + repack_score + bitrate_bonus
    return int(total_score)

def update_duplicate_groups(protected_ids: Optional[Iterable[str]] = None):
    """
    Scans media_items in PostgreSQL, identifies titles/episodes with > 1 instance,
    and updates duplicate_groups table.

    protected_ids: MediaItem ids never purged as "file missing" (e.g. a file
    mid-swap by the transcoder, briefly absent from its old path).
    """
    protected = set(protected_ids or ())
    session = SyncSessionLocal()
    try:
        # Files mid-swap by the transcoder briefly vanish from their old path.
        from media_manager.web.services.transcode_jobs import swap_locked_sync
        try:
            protected |= swap_locked_sync(session)["item_ids"]
        except Exception as e:
            logger.warning(f"Could not read transcode swap locks: {e}")
        # Group items by title and season/episode
        items = session.query(MediaItem).all()

        # Purge rows whose backing file no longer exists on disk before
        # grouping. A stale row (e.g. left behind when a file was renamed
        # elsewhere) would otherwise look like a duplicate of the file's
        # real, current location - and tie-break logic elsewhere could end
        # up keeping the stale row while deleting the real file.
        #
        # SAFETY: only trust a missing-file result for a storage_volume whose
        # top-level directory is itself currently reachable and non-empty. A
        # volume that's unreadable or (unexpectedly) empty almost always means
        # its mount dropped - an NFS/CIFS remount race after a reboot, a
        # temporary disconnect, systemd hitting an automount's start-limit -
        # not that every file under it was actually deleted. Treating "the
        # mount is down" the same as "these files are gone" is exactly what
        # turned a transient mount outage into thousands of permanently
        # deleted database rows in production; skip the volume instead and
        # let a later, healthy scan/duplicate-check catch genuinely-removed
        # files once the mount is confirmed back.
        volume_health: Dict[str, bool] = {}

        def _volume_ok(volume: str) -> bool:
            if volume not in volume_health:
                volume_health[volume] = volume_is_healthy(volume)
            return volume_health[volume]

        stale_items = []
        skipped_volumes = set()
        for item in items:
            if item.id in protected or os.path.exists(item.path):
                continue
            if _volume_ok(item.storage_volume):
                stale_items.append(item)
            else:
                skipped_volumes.add(item.storage_volume)

        if skipped_volumes:
            logger.warning(
                f"Skipping stale-file purge for storage volumes that look unmounted/empty "
                f"right now (their MediaItem rows were left as-is, not assumed deleted): "
                f"{sorted(skipped_volumes)}"
            )

        if stale_items:
            for item in stale_items:
                logger.info(f"Purging stale MediaItem (file missing on disk): {item.path}")
                session.delete(item)
            items = [item for item in items if item not in stale_items]

        grouped: Dict[str, List[MediaItem]] = {}

        display_names: Dict[str, str] = {}
        for item in items:
            if item.media_type == "tv" and item.season is not None and item.episode is not None:
                series = tv_series_name(item.path, item.title)
                key = f"tv:{_series_key(series)}:S{item.season or 0}E{item.episode or 0}"
                display_names.setdefault(key, series)
            else:
                # Unnumbered TV keeps the title (episode title) key: a series
                # key would put every unnumbered episode of a show in one group.
                key = f"{item.media_type}:{item.title.lower()}"
                if item.media_type == "tv":
                    key += f":S{item.season or 0}E{item.episode or 0}"
            grouped.setdefault(key, []).append(item)

        for key, group_items in grouped.items():
            if len(group_items) > 1:
                # Duplicate found! Sort by quality score DESC
                group_items.sort(key=lambda x: x.quality_score, reverse=True)
                best_item = group_items[0]

                se_label = f"S{best_item.season:02d}E{best_item.episode:02d}" if best_item.media_type == "tv" and best_item.season is not None else "Movie"
                group_title = display_names.get(key, best_item.title)
                
                dup_group = session.query(DuplicateGroup).filter_by(group_key=key).first()
                if not dup_group:
                    dup_group = DuplicateGroup(
                        group_key=key,
                        media_title=group_title,
                        season_episode=se_label,
                        best_media_id=best_item.id,
                        total_count=len(group_items)
                    )
                    session.add(dup_group)
                    # Flush so the new group has its id before items link to
                    # it; otherwise they get duplicate_group_id=None until the
                    # next run.
                    session.flush()
                else:
                    dup_group.media_title = group_title
                    dup_group.season_episode = se_label
                    dup_group.best_media_id = best_item.id
                    dup_group.total_count = len(group_items)

                for item in group_items:
                    item.duplicate_group_id = dup_group.id
            else:
                for item in group_items:
                    item.duplicate_group_id = None

        # Drop groups whose key no longer has duplicates, including keys from
        # an older grouping scheme that will never be produced again.
        live_keys = {k for k, v in grouped.items() if len(v) > 1}
        for dup_group in session.query(DuplicateGroup).all():
            if dup_group.group_key not in live_keys:
                session.delete(dup_group)

        session.commit()
        logger.info("Duplicate groups updated successfully.")
    except Exception as e:
        session.rollback()
        logger.error(f"Failed to update duplicate groups: {e}")
    finally:
        session.close()
