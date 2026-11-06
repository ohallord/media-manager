# Tests Package Initialization
import os

# Guard against ever running the suite against the real production database.
# media_manager/config.py defaults DB_PATH to media_manager/media.db, which on
# a homelab checkout is the live DB media-manager-watcher.service has open -
# several tests write/delete real rows (users, media items, directories).
# Always run via scripts/run_tests.sh, which exports an isolated DB_PATH
# before the interpreter starts (setting it from Python here would be too
# late for `main.py test`, whose click group binds the DB engine at import
# time, before this package is ever imported).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_DB_PATH = os.path.normpath(os.path.join(_PROJECT_ROOT, "media_manager", "media.db"))
_ACTIVE_DB_PATH = os.path.normpath(os.environ.get("DB_PATH", _DEFAULT_DB_PATH))

if _ACTIVE_DB_PATH == _DEFAULT_DB_PATH:
    raise RuntimeError(
        "Refusing to run the test suite against the default production "
        f"database ({_DEFAULT_DB_PATH}). Run tests via `scripts/run_tests.sh` "
        "instead, or set DB_PATH to an isolated file before invoking the "
        "test suite directly."
    )
