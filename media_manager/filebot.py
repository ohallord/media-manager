import os
import subprocess
import logging
from .config import FILEBOT_SCRIPTS_PATH

import re

MOVIE_FORMAT = "{n}/{ny}"

# Sonarr's episode naming: "Tracker (2024) - 4x02 - Switchback HDTV-720p.mkv".
_SONARR_EPISODE = re.compile(r"^(?P<series>[^{}/\\]+?) - \d+x\d+ - ")


# Full Sonarr episode name, with its trailing quality tag ("HDTV-720p Proper").
_SONARR_EPISODE_FULL = re.compile(
    r"^(?P<series>[^{}/\\]+?) - (?P<season>\d+)x(?P<episode>\d+) - (?P<title>.+?)"
    r"(?: (?:WEBDL|WEBRip|HDTV|Bluray|Bluray Remux|Remux|SDTV|DVD|Raw-HD|Unknown)"
    r"(?:-\d+p)?(?: Proper| REPACK)*)?$",
    re.IGNORECASE,
)


def sonarr_series_title(file_path):
    """Sonarr's series title from its episode filename, or None."""
    m = _SONARR_EPISODE.match(os.path.basename(file_path))
    return m.group("series").strip() if m else None


def series_format(library_root, series_title=None):
    """
    "Show/Season 1/1x05 - Title" relative to the library root.

    With Sonarr's title the folder is that title ("Tracker (2024)",
    "Ghosts (US)"), which is what the library uses. Otherwise FileBot's name
    is used, preferring an existing "Show (Year)" folder since FileBot names
    some shows without their year.
    """
    if series_title:
        return f"{series_title}/Season {{s}}/{{sxe}} - {{t}}"
    root = library_root.replace("\\", "\\\\").replace('"', '\\"')
    return (
        '{ def d = n + " (" + y + ")"; '
        f'new File("{root}", d).isDirectory() ? d : n }}'
        "/Season {s}/{sxe} - {t}"
    )

def file_sonarr_episode(file_path):
    """
    Fallback for when FileBot can't identify a Sonarr-named episode (it groups
    "Doc (US) - 3x02 - ..." as Series=null): move it to
    "<root>/<Show>/Season <s>/<sxe> - <title>.ext" from the name alone, so it
    doesn't sit in the library root where Plex never lists it. Never
    overwrites; returns [dest] or [].
    """
    if not os.path.isfile(file_path):
        return []
    name, ext = os.path.splitext(os.path.basename(file_path))
    m = _SONARR_EPISODE_FULL.match(name)
    if not m:
        return []
    season = int(m.group("season"))
    dest = os.path.join(
        os.path.dirname(file_path),
        m.group("series").strip(),
        f"Season {season}",
        f"{season}x{m.group('episode')} - {m.group('title').strip()}{ext}",
    )
    # Same episode under a different title ("W.W.D.D." vs "W.W.D.D") counts.
    season_dir = os.path.dirname(dest)
    prefix = f"{season}x{m.group('episode')} - "
    if os.path.isdir(season_dir) and any(
        f.startswith(prefix) and os.path.splitext(f)[1].lower() == ext.lower()
        for f in os.listdir(season_dir)
    ):
        logging.info(f"[SONARR_FILE_SKIP] {prefix.strip(' -')} already in {season_dir}; leaving {file_path}")
        return []
    from .config import UID, GID, PERMISSIONS
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.rename(file_path, dest)
    except OSError as e:
        logging.error(f"[SONARR_FILE_FAILED] Could not move {file_path} to {dest}: {e}")
        return []
    try:
        os.chown(dest, UID, GID)
        os.chmod(dest, PERMISSIONS)
        for d in (os.path.dirname(dest), os.path.dirname(os.path.dirname(dest))):
            os.chown(d, UID, GID)
            os.chmod(d, 0o775)
    except OSError as perm_err:
        logging.warning(f"Could not apply permissions for {dest}: {perm_err}")
    logging.info(f"[SONARR_FILED] FileBot couldn't identify {file_path}; filed as {dest}")
    return [dest]


def run_filebot_artwork(file_path):
    """
    Runs FileBot on a file to fetch artwork and subtitles.
    Returns a list of destination paths if FileBot moved/renamed the file.
    """
    try:
        if not os.path.exists(file_path):
            logging.error(f"File {file_path} not found for FileBot.")
            return []
            
        # We can use the user's amc script or just standard filebot
        # Since we already moved the file to its destination, we just want it to get artwork/CC
        
        # Example using amc:
        # filebot -script fn:amc <path> --output <parent_dir> --action keeplink --conflict auto --def artwork=y subtitles=en
        
        # Or just specific artwork script if they prefer
        # filebot -script $FILEBOT_SCRIPTS_PATH/artwork.groovy <path>
        
        dest_dir = os.path.dirname(file_path)
        series_title = sonarr_series_title(file_path)

        cmd = [
            "filebot",
            "-script", "fn:amc",
            file_path,
            "--output", dest_dir,
            "--action", "move",
            "--conflict", "auto",
            "--def", "artwork=y", "subtitles=en,es", "clean=y",
            # Same layout as the library itself (and the host's scheduled
            # `filebot -rename -r` jobs), relative to the library root:
            # "Show/Season 1/1x05 - Title.mkv", "Movie/Movie (2000).mkv".
            f"seriesFormat={series_format(dest_dir, series_title)}",
            f"movieFormat={MOVIE_FORMAT}"
        ]
        if series_title:
            # Strict mode gives up on ambiguous names ("American Hostage" vs
            # two "Hostage" shows, two "Tracker" shows), leaving the file loose
            # in the library root. Querying Sonarr's exact title resolves it;
            # -non-strict instead guesses and misfiles (e.g. "Brothers (2026)").
            cmd[3:3] = ["--q", series_title]
        
        logging.info(f"[FILEBOT_START] Running FileBot AMC for {file_path}...")
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        if result.stdout:
            logging.info(f"[FILEBOT_OUTPUT] Output for {file_path}:\n{result.stdout}")
        if result.stderr:
            logging.warning(f"[FILEBOT_WARNING] Stderr for {file_path}:\n{result.stderr}")
            
        dest_paths = []
        from .config import UID, GID, PERMISSIONS

        # Parse moved paths, e.g. [MOVE] from [/src] to [/dst]
        matches = re.findall(r"\[(?:MOVE|RENAME|COPY)\]\s+from\s+\[.*?\]\s+to\s+\[(.*?)\]", result.stdout)
        for match in matches:
            if os.path.exists(match):
                logging.info(f"[FILEBOT_MOVED] FileBot moved file to {match}")
                dest_paths.append(match)
                
                # Apply correct ownership and permissions recursively
                try:
                    # Apply to moved file/artwork/subtitle
                    if os.path.isfile(match):
                        os.chown(match, UID, GID)
                        os.chmod(match, PERMISSIONS)
                    
                    # Apply to containing parent folder (e.g. "Season 1")
                    parent_dir = os.path.dirname(match)
                    if os.path.exists(parent_dir):
                        os.chown(parent_dir, UID, GID)
                        os.chmod(parent_dir, 0o775)
                except Exception as perm_err:
                    logging.warning(f"Could not apply FileBot permissions for {match}: {perm_err}")
                
        logging.info(f"[FILEBOT_FINISH] FileBot finished for {file_path}. Resulting paths: {dest_paths}")
        return dest_paths
        
    except Exception as e:
        logging.error(f"Error running FileBot for {file_path}: {e}")
        return []
