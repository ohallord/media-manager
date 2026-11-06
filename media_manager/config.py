import json
import os
from typing import List, Dict

# Single source of truth for the app version - shared by the API
# (FastAPI app version + GET /api/v1/version) and the web UI footer.
_VERSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")
try:
    with open(_VERSION_FILE) as _f:
        VERSION = _f.read().strip()
except FileNotFoundError:
    VERSION = "0.0.0-dev"

# Database: SQLite by default (`DB_PATH`). Set USE_POSTGRES=true + POSTGRES_URI for Postgres.
POSTGRES_URI = os.getenv("POSTGRES_URI", "postgresql+asyncpg://mediauser:mediapass@localhost:5432/mediadb")
SYNC_POSTGRES_URI = os.getenv("SYNC_POSTGRES_URI", POSTGRES_URI.replace("+asyncpg", "+psycopg2") if "+asyncpg" in POSTGRES_URI else POSTGRES_URI)

# Security & WebAuthn / Passkey Secrets
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = int(os.getenv("JWT_EXPIRATION_HOURS", "720"))
RP_ID = os.getenv("RP_ID", "localhost")
RP_NAME = os.getenv("RP_NAME", "Media Manager Portal")
INITIAL_ADMIN_USER = os.getenv("INITIAL_ADMIN_USER", "admin")
# No default: if unset, first-login admin bootstrap is disabled (fail closed).
# setup.sh generates one into .env; Helm reads it from the chart Secret.
INITIAL_ADMIN_PASSWORD = os.getenv("INITIAL_ADMIN_PASSWORD") or None

# Web Server Settings (Defaulting to 8088 to avoid conflict with Portainer on 8000)
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8088"))

# Media directories to scan. Example defaults; set MEDIA_DIRECTORIES
# (comma-separated) for your library.
DEFAULT_MEDIA_DIRS = [
    "/media/Movies",
    "/media/TvShows",
]
env_dirs = os.getenv("MEDIA_DIRECTORIES")
MEDIA_DIRECTORIES: List[str] = env_dirs.split(",") if env_dirs else DEFAULT_MEDIA_DIRS

# Source directories for the automated watcher: a JSON array of
# {"from_dir", "to_dir", "type"} objects, e.g.
#   SOURCE_DIRECTORIES='[{"from_dir":"/downloads/movies","to_dir":"/media/Movies","type":"movie"}]'
# The defaults below are examples; set SOURCE_DIRECTORIES for your setup.
DEFAULT_SOURCE_DIRECTORIES: List[Dict[str, str]] = [
    {"from_dir": "/downloads/movies", "to_dir": "/media/Movies/", "type": "movie"},
    {"from_dir": "/downloads/tv", "to_dir": "/media/TvShows/", "type": "tv"},
]
env_source_dirs = os.getenv("SOURCE_DIRECTORIES")
SOURCE_DIRECTORIES: List[Dict[str, str]] = (
    json.loads(env_source_dirs) if env_source_dirs else DEFAULT_SOURCE_DIRECTORIES
)

# FileBot Scripts Path (if available)
FILEBOT_SCRIPTS_PATH = os.getenv("FILEBOT_SCRIPTS_PATH", os.path.expanduser("~/filebot_scripts"))

# Video & Audio Extensions
VIDEO_EXTENSIONS = [".avi", ".mpg", ".mkv", ".divx", ".vob", ".mp4", ".m4v", ".m2ts", ".iso"]
AUDIO_EXTENSIONS = [".mp3", ".ogg", ".flac", ".wav"]

# Fallback DB Path for SQLite backward compatibility
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "media.db"))

# Ownership & Permissions
UID = int(os.getenv("UID", "1000"))
GID = int(os.getenv("GID", "1000"))
PERMISSIONS = 0o664
