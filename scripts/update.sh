#!/usr/bin/env bash
#
# Fast in-place update for an already-installed DualStream deployment.
#
# Use this after a `git pull` to push the new source into the running
# venv and bounce the service, without running the full installer (which
# does apt updates and other slow / one-time work).
#
# Usage:
#   cd /path/to/cloned/DualStream
#   git pull
#   sudo bash scripts/update.sh
#
# This is equivalent to the relevant parts of install.sh's setup_venv
# and the systemctl restart at the end. Re-running it is safe.

set -euo pipefail

USER_NAME="dualstream"
INSTALL_PREFIX="/opt/dualstream"
SERVICE_NAME="dualstream"

log() { printf '[update] %s\n' "$*"; }

require_root() {
    if [[ "$(id -u)" -ne 0 ]]; then
        echo "ERROR: this script must be run as root (sudo bash $0)" >&2
        exit 1
    fi
}

require_root

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -d "${INSTALL_PREFIX}/.venv" ]]; then
    echo "ERROR: ${INSTALL_PREFIX}/.venv does not exist." >&2
    echo "Run scripts/install.sh first." >&2
    exit 1
fi

log "Re-installing dualstream from ${REPO_ROOT}"
# --force-reinstall + --no-deps: rebuild the dualstream package without
# touching its already-installed pip dependencies (much faster than a
# clean install, and avoids pip re-resolving aiortc et al.).
"${INSTALL_PREFIX}/.venv/bin/pip" install \
    --force-reinstall \
    --no-deps \
    "${REPO_ROOT}"

log "Setting ownership of ${INSTALL_PREFIX} to ${USER_NAME}"
chown -R "${USER_NAME}:${USER_NAME}" "${INSTALL_PREFIX}"

log "Restarting ${SERVICE_NAME}.service"
systemctl restart "${SERVICE_NAME}.service"

# Brief health check.
sleep 1
if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
    log "Service is running."
else
    log "WARNING: ${SERVICE_NAME}.service is not active. Inspect:"
    log "    sudo journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
    exit 1
fi

log "Update complete."
