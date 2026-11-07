#!/bin/bash
# Runs the test suite against a throwaway SQLite DB so it never touches the
# real media_manager/media.db (which media-manager-watcher.service may have
# open on this host - see docs/SYSTEM_DESIGN.md "Automated Testing" for why this
# exists). DB_PATH must be exported before python starts: main.py's click
# group initializes the DB engine at import time, before any subcommand runs,
# so setting os.environ from inside Python is too late for that entrypoint.
set -euo pipefail

cd "$(dirname "$0")/.."

TMP_DB="$(mktemp -u "${TMPDIR:-/tmp}/media_manager_test_XXXXXX.db")"
export DB_PATH="$TMP_DB"
export WATCHER_HEARTBEAT_HTTP=0
# Ephemeral signing key so config never persists a .jwt_secret next to the temp DB.
export JWT_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))' 2>/dev/null || openssl rand -hex 32)"
trap 'rm -f "$TMP_DB"' EXIT

echo "Running tests against isolated DB: $DB_PATH" >&2
python3 -c "from media_manager.database import init_db_schema_sync; init_db_schema_sync()"

if [ "${1:-}" = "--coverage" ]; then
    shift
    # Don't let set -e skip the report on test failures - still want
    # coverage output to inspect what's failing, but the script must still
    # exit non-zero overall so CI/callers see the failure.
    set +e
    coverage run -m unittest discover -s tests -t . -p 'test_*.py' "$@"
    test_status=$?
    set -e
    coverage report -m
    exit "$test_status"
else
    python3 -m unittest discover -s tests -t . -p 'test_*.py' "$@"
fi
