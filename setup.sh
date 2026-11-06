#!/bin/bash
# Guided first-time setup for Media Manager: checks prerequisites, creates a
# virtualenv, installs dependencies, generates a .env with a real JWT secret
# and admin credentials, initializes the database, and optionally installs
# the watcher/web-portal as systemd services. Safe to re-run - existing
# .env and systemd services are left alone rather than overwritten.
#
# See docs/INSTALL.md for what each step does and how to do it manually.
#
# Non-interactive: `./setup.sh --non-interactive` (or SETUP_NONINTERACTIVE=1)
# takes every default - admin user "admin", a generated password, port 8088,
# no systemd services. PYTHON=python3.12 picks the interpreter for the venv.
set -e

NONINTERACTIVE="${SETUP_NONINTERACTIVE:-0}"
for arg in "$@"; do
    case "$arg" in
        --non-interactive|-y) NONINTERACTIVE=1 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done
PYTHON="${PYTHON:-python3}"

# ask VAR "prompt" [default] - reads into VAR, or takes the default when
# non-interactive.
ask() {
    if [ "$NONINTERACTIVE" = "1" ]; then
        printf -v "$1" '%s' "${3:-}"
    else
        read -r -p "$2" "$1"
    fi
}

cd "$(dirname "$0")"
PROJECT_DIR=$(pwd)

if [ ! -f "main.py" ] || [ ! -d "media_manager" ]; then
    echo "Error: run this from the project root (where main.py is located)."
    exit 1
fi

echo "=== Media Manager Setup ==="
echo ""

echo "--- 1. Checking prerequisites ---"
MISSING=()
command -v "$PYTHON" >/dev/null || MISSING+=("$PYTHON")
command -v ffmpeg >/dev/null || MISSING+=("ffmpeg")
command -v git >/dev/null || MISSING+=("git")
if [ ${#MISSING[@]} -gt 0 ]; then
    echo "Missing required tools: ${MISSING[*]}"
    echo "On Debian/Ubuntu, install with:"
    echo "  sudo apt-get update && sudo apt-get install -y python3 python3-venv ffmpeg git"
    exit 1
fi
echo "[OK] $PYTHON, ffmpeg, git found."

echo ""
echo "--- 2. Python virtual environment & dependencies ---"
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment at .venv ..."
    if ! "$PYTHON" -m venv .venv 2>/tmp/venv_err.log; then
        cat /tmp/venv_err.log
        echo ""
        echo "Failed to create a virtual environment - python3-venv is likely missing."
        echo "On Debian/Ubuntu:  sudo apt-get install -y python3-venv"
        rm -f /tmp/venv_err.log
        exit 1
    fi
    rm -f /tmp/venv_err.log
else
    echo "[SKIP] .venv already exists."
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
echo "[OK] Dependencies installed."

echo ""
echo "--- 3. Configuration (.env) ---"
if [ -f .env ]; then
    echo "[SKIP] .env already exists, leaving it as-is."
    # shellcheck disable=SC1091
    set -a; source .env; set +a
else
    JWT_SECRET_GEN=$(openssl rand -hex 32 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex(32))")

    ask ADMIN_USER_IN "Initial admin username [admin]: " admin
    ADMIN_USER_IN=${ADMIN_USER_IN:-admin}

    ADMIN_PASS_IN=""
    if [ "$NONINTERACTIVE" != "1" ]; then
        read -r -s -p "Initial admin password (leave blank to auto-generate a strong one): " ADMIN_PASS_IN
        echo ""
    fi
    if [ -z "$ADMIN_PASS_IN" ]; then
        ADMIN_PASS_IN=$(openssl rand -base64 18 2>/dev/null || python3 -c "import secrets; print(secrets.token_urlsafe(18))")
        echo "Generated admin password: $ADMIN_PASS_IN"
        echo "(This is only shown once - it's saved in .env, but write it down.)"
    fi

    ask WEB_PORT_IN "Web server port [8088]: " 8088
    WEB_PORT_IN=${WEB_PORT_IN:-8088}

    cat > .env <<EOF
JWT_SECRET=$JWT_SECRET_GEN
INITIAL_ADMIN_USER=$ADMIN_USER_IN
INITIAL_ADMIN_PASSWORD=$ADMIN_PASS_IN
WEB_PORT=$WEB_PORT_IN
EOF
    chmod 600 .env
    echo "[OK] Wrote .env (permissions 600)."

    # shellcheck disable=SC1091
    set -a; source .env; set +a
fi

echo ""
echo "Reminder: MEDIA_DIRECTORIES and SOURCE_DIRECTORIES default to example"
echo "paths. Add your own to .env - see .env.example and the Configuration"
echo "reference in docs/INSTALL.md."

echo ""
echo "--- 4. Database schema ---"
python3 -c "from media_manager.database import init_db_schema_sync; init_db_schema_sync()"
echo "[OK] Database schema ready."

echo ""
echo "--- 5. Watcher service (optional) ---"
echo "Monitors SOURCE_DIRECTORIES and moves completed downloads into your"
echo "library. Skip this if you only want the web portal."
ask INSTALL_WATCHER "Install the file-watcher as a systemd service now? [y/N] " n
if [[ "$INSTALL_WATCHER" =~ ^[Yy]$ ]]; then
    ./install.sh
else
    echo "Skipped. Run ./install.sh later if you change your mind."
fi

echo ""
echo "--- 6. Web portal service (optional) ---"
ask INSTALL_WEB "Install the web portal as a systemd service now? [y/N] " n
if [[ "$INSTALL_WEB" =~ ^[Yy]$ ]]; then
    SERVICE_NAME="media-manager-web"
    SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
    PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python3"
    RUN_USER=$(whoami)

    cat <<EOF | sudo tee "$SERVICE_FILE" > /dev/null
[Unit]
Description=Media Manager Web Portal
After=network.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=-${PROJECT_DIR}/.env
ExecStart=${PYTHON_BIN} main.py serve --host 0.0.0.0 --port ${WEB_PORT:-8088}
Restart=always
RestartSec=10
StandardOutput=append:${PROJECT_DIR}/media_manager_web.log
StandardError=append:${PROJECT_DIR}/media_manager_web.log

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME"
    sudo systemctl start "$SERVICE_NAME"
    sudo systemctl status "$SERVICE_NAME" --no-pager
    echo "[OK] Web portal service installed and started."
else
    echo "Skipped. Start it manually with:"
    echo "  source .venv/bin/activate && set -a && source .env && set +a && python3 main.py serve"
fi

echo ""
echo "=== Setup complete ==="
echo "Open http://localhost:${WEB_PORT:-8088} and log in with:"
echo "  Username: ${INITIAL_ADMIN_USER:-admin}"
echo "  Password: (whatever you set/generated above - also in .env)"
echo ""
echo "Then go to Admin -> Integrations to connect Radarr/Sonarr/SABnzbd/TMDb/"
echo "Fanart.tv/Email, and Admin -> Users to create real accounts."
echo "Full reference: docs/INSTALL.md"
