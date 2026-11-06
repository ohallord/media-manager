#!/bin/bash

# Exit on error
set -e

# Ensure we are in the directory of the script
cd "$(dirname "$0")"
PROJECT_DIR=$(pwd)

if [ ! -f "main.py" ] || [ ! -d "media_manager" ]; then
    echo "Error: Please run this script from the project root (where main.py is located)."
    exit 1
fi

SERVICE_NAME="media-manager-watcher"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PYTHON_BIN=$(which python3)
USER=$(whoami)

echo "Installing Media Manager Watcher as a systemd service..."

# Create the service file
cat <<EOF | sudo tee ${SERVICE_FILE} > /dev/null
[Unit]
Description=Media Manager File Watcher
After=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${PROJECT_DIR}
ExecStart=${PYTHON_BIN} main.py watch
Environment=WATCHER_HEARTBEAT_URL=http://127.0.0.1:18088/api/v1/watcher/heartbeat
Environment=WATCHER_HEARTBEAT_TOKEN=media-manager-watcher
Environment=WATCHER_HEARTBEAT_TLS_VERIFY=false
Restart=always
RestartSec=10
StandardOutput=append:${PROJECT_DIR}/media_manager.log
StandardError=append:${PROJECT_DIR}/media_manager.log

[Install]
WantedBy=multi-user.target
EOF

echo "Reloading systemd daemon..."
sudo systemctl daemon-reload

echo "Enabling ${SERVICE_NAME}..."
sudo systemctl enable ${SERVICE_NAME}

echo "Starting ${SERVICE_NAME}..."
sudo systemctl start ${SERVICE_NAME}

echo "Service status:"
sudo systemctl status ${SERVICE_NAME} --no-pager

echo ""
echo "Installation complete. The watcher is now running and will start automatically on boot."
echo "Logs are being written to ${PROJECT_DIR}/media_manager.log"
echo "You can manage the service with:"
echo "  sudo systemctl start ${SERVICE_NAME}"
echo "  sudo systemctl stop ${SERVICE_NAME}"
echo "  sudo systemctl status ${SERVICE_NAME}"
echo "  sudo journalctl -u ${SERVICE_NAME} -f"
