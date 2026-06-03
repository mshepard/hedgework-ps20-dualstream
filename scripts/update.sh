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
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
# How long to wait for graceful stop before SIGKILLing the unit. Should
# match (or be slightly larger than) ``TimeoutStopSec=`` in the service
# unit; gives systemd a chance to do the right thing before we step in.
STOP_TIMEOUT=12

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

# Keep the installed unit in sync with the repo. Cheap on a no-op, and
# guarantees TimeoutStopSec / KillMode tweaks reach the live system on
# the very next stop.
if [[ -f "${REPO_ROOT}/scripts/dualstream.service" ]]; then
    if ! cmp -s "${REPO_ROOT}/scripts/dualstream.service" "${SERVICE_FILE}"; then
        log "Updating ${SERVICE_FILE}"
        install -o root -g root -m 0644 \
            "${REPO_ROOT}/scripts/dualstream.service" "${SERVICE_FILE}"
        systemctl daemon-reload
    fi
fi

# Use stop+start (with a force-kill fallback) rather than `restart`, so
# we don't sit waiting for systemd's default 90s TimeoutStopSec if the
# running process is wedged inside picamera2's libcamera pipeline.
log "Stopping ${SERVICE_NAME}.service (timeout ${STOP_TIMEOUT}s)"
if ! timeout "${STOP_TIMEOUT}" systemctl stop "${SERVICE_NAME}.service"; then
    log "WARNING: graceful stop exceeded ${STOP_TIMEOUT}s; force-killing"
    systemctl kill -s KILL "${SERVICE_NAME}.service" || true
    # Give systemd a beat to mark the unit inactive after SIGKILL.
    for _ in 1 2 3 4 5; do
        if ! systemctl is-active --quiet "${SERVICE_NAME}.service"; then
            break
        fi
        sleep 1
    done
fi

log "Starting ${SERVICE_NAME}.service"
systemctl start "${SERVICE_NAME}.service"

sleep 1
if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
    log "Service is running."
else
    log "WARNING: ${SERVICE_NAME}.service is not active. Inspect:"
    log "    sudo journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
    exit 1
fi

log "Update complete."
