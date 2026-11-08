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
# No default URI: baked-in credentials would silently connect to whatever answers
# on localhost. database.py fails closed when USE_POSTGRES=true and this is empty.
def _postgres_uri_from_env() -> str:
    return os.getenv("POSTGRES_URI", "").strip()


def _sync_postgres_uri(uri: str) -> str:
    return uri.replace("+asyncpg", "+psycopg2") if "+asyncpg" in uri else uri


POSTGRES_URI = _postgres_uri_from_env()
SYNC_POSTGRES_URI = os.getenv("SYNC_POSTGRES_URI") or _sync_postgres_uri(POSTGRES_URI)

# Security & WebAuthn / Passkey Secrets
# JWT_SECRET is resolved below, after DB_PATH (see _resolve_jwt_secret).
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

# The old hardcoded fallback is public, so it is treated exactly like "unset".
_LEGACY_JWT_SECRET = "dev-secret-key-change-in-production"


def _resolve_jwt_secret(env_value, secret_file):
    """
    Fail-safe JWT signing key. An explicit JWT_SECRET wins. Otherwise use a random
    key persisted (mode 0600) at secret_file so sessions survive restarts; if that
    file cannot be written, use an ephemeral random key (everyone is logged out on
    restart, but tokens can never be forged with a known key).
    """
    import secrets as _secrets
    if env_value and env_value != _LEGACY_JWT_SECRET:
        return env_value
    if secret_file:
        try:
            with open(secret_file, encoding="utf-8") as f:
                existing = f.read().strip()
            if len(existing) >= 32:
                return existing
        except OSError:
            pass
        generated = _secrets.token_urlsafe(48)
        try:
            fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(generated)
            os.chmod(secret_file, 0o600)
            return generated
        except OSError:
            pass
    return _secrets.token_urlsafe(48)


JWT_SECRET_FILE = os.getenv("JWT_SECRET_FILE") or (
    os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), ".jwt_secret") if DB_PATH != ":memory:" else None
)
JWT_SECRET = _resolve_jwt_secret(os.getenv("JWT_SECRET"), JWT_SECRET_FILE)

# Ownership & Permissions
UID = int(os.getenv("UID", "1000"))
GID = int(os.getenv("GID", "1000"))
PERMISSIONS = 0o664
