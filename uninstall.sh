#!/bin/bash

# Exit on error
set -e

SERVICE_NAME="media-manager-watcher"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo "Uninstalling Media Manager Watcher..."

if [ -f "${SERVICE_FILE}" ]; then
    echo "Stopping ${SERVICE_NAME}..."
    sudo systemctl stop ${SERVICE_NAME} || true
    
    echo "Disabling ${SERVICE_NAME}..."
    sudo systemctl disable ${SERVICE_NAME} || true
    
    echo "Removing service file..."
    sudo rm "${SERVICE_FILE}"
    
    echo "Reloading systemd daemon..."
    sudo systemctl daemon-reload
    
    echo "Uninstallation complete."
else
    echo "Service ${SERVICE_NAME} is not installed."
fi

# The H.265 transcode worker (scripts/install_transcoder.sh), if installed.
TRANSCODER_FILE="/etc/systemd/system/media-manager-transcoder.service"
if [ -f "${TRANSCODER_FILE}" ]; then
    echo "Removing media-manager-transcoder..."
    sudo systemctl stop media-manager-transcoder || true
    sudo systemctl disable media-manager-transcoder || true
    sudo rm "${TRANSCODER_FILE}"
    sudo systemctl daemon-reload
fi
