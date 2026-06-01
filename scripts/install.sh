#!/usr/bin/env bash
#
# DualStream Phase 1 installer for Raspberry Pi 5 (Raspberry Pi OS Bookworm).
#
# Usage: sudo bash scripts/install.sh [--install-tailscale]
#
# What this does:
#   * Installs apt dependencies (picamera2, libcamera, ffmpeg, venv tooling)
#   * Optionally installs Tailscale
#   * Creates the dualstream system user (member of `video`)
#   * Creates /etc/dualstream/ and /var/lib/dualstream/snapshots
#   * Builds a venv at /opt/dualstream/.venv with --system-site-packages
#     so it inherits the apt-installed python3-picamera2
#   * pip-installs this project into the venv
#   * Installs and enables the dualstream systemd unit
#   * On first install (or wherever the placeholder values are still in the
#     config), auto-generates random admin/viewer tokens and tightens the
#     config file mode to 0640 root:dualstream
#
# What this does NOT do (deliberately — these are hardware-sensitive):
#   * Modify /boot/firmware/config.txt (CSI dtoverlays). See README.
#   * Run `tailscale up`. You'll do that with your own auth flow.
#
# Re-running this script is safe; it's idempotent.

set -euo pipefail

INSTALL_TAILSCALE=0
if [[ "${1:-}" == "--install-tailscale" ]]; then
    INSTALL_TAILSCALE=1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="dualstream"
INSTALL_PREFIX="/opt/dualstream"
CONFIG_DIR="/etc/dualstream"
DATA_DIR="/var/lib/dualstream"
SNAPSHOT_DIR="${DATA_DIR}/snapshots"
SERVICE_FILE="/etc/systemd/system/dualstream.service"

require_root() {
    if [[ "$(id -u)" -ne 0 ]]; then
        echo "ERROR: this installer must be run as root (sudo bash $0)" >&2
        exit 1
    fi
}

log() { printf '[install] %s\n' "$*"; }

check_platform() {
    if [[ ! -e /proc/device-tree/model ]]; then
        log "WARNING: /proc/device-tree/model missing; cannot confirm platform"
        return
    fi
    local model
    model=$(tr -d '\0' </proc/device-tree/model)
    log "Detected platform: ${model}"
    if [[ "${model}" != *"Raspberry Pi 5"* ]]; then
        log "WARNING: this project targets Raspberry Pi 5. Detected '${model}'."
        log "Continuing anyway, but picamera2 / CSI behaviour may differ."
    fi
}

install_apt_deps() {
    log "Updating apt and installing system dependencies"
    DEBIAN_FRONTEND=noninteractive apt-get update -y
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        python3-picamera2 \
        libcamera-tools \
        libcamera-ipa \
        ffmpeg \
        ca-certificates \
        curl
}

install_tailscale() {
    if (( INSTALL_TAILSCALE == 0 )); then
        log "Skipping Tailscale install (pass --install-tailscale to enable)"
        return
    fi
    if command -v tailscale >/dev/null 2>&1; then
        log "Tailscale already installed"
        return
    fi
    log "Installing Tailscale"
    curl -fsSL https://tailscale.com/install.sh | sh
    log "Run 'sudo tailscale up' to authenticate with your tailnet"
}

create_user() {
    if ! id "${USER_NAME}" >/dev/null 2>&1; then
        log "Creating system user ${USER_NAME}"
        useradd --system --home-dir "${INSTALL_PREFIX}" --shell /usr/sbin/nologin \
            --groups video "${USER_NAME}"
    else
        log "User ${USER_NAME} already exists"
        # Ensure video group membership
        usermod -aG video "${USER_NAME}" || true
    fi
}

create_directories() {
    log "Creating ${INSTALL_PREFIX}, ${CONFIG_DIR}, ${SNAPSHOT_DIR}"
    install -d -o "${USER_NAME}" -g "${USER_NAME}" -m 0755 "${INSTALL_PREFIX}"
    install -d -o root -g root -m 0755 "${CONFIG_DIR}"
    install -d -o "${USER_NAME}" -g "${USER_NAME}" -m 0755 "${DATA_DIR}" "${SNAPSHOT_DIR}"
}

generate_token() {
    # 32 url-safe characters → 256 bits of entropy. The output uses only
    # [A-Za-z0-9_-], so it's safe inside TOML double-quoted strings.
    python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
}

# Replace one TOML string-valued key on a line of the form
#     <key> = "<placeholder>"
# with a freshly generated random token, only if the line still contains
# the literal placeholder. Idempotent across re-runs (a customised value
# is left alone). Uses Python for the rewrite rather than sed so it works
# on both GNU and BSD platforms and so the token never appears in argv.
#
# Arguments: $1 = config path, $2 = key name, $3 = placeholder value.
# Prints the substituted token on stdout (empty string if no change made).
rotate_placeholder_token() {
    local cfg="$1"
    local key="$2"
    local placeholder="$3"
    CFG="${cfg}" KEY="${key}" PLACEHOLDER="${placeholder}" python3 - <<'PY'
import os
import re
import secrets
import sys

cfg = os.environ["CFG"]
key = os.environ["KEY"]
placeholder = os.environ["PLACEHOLDER"]

with open(cfg, "r", encoding="utf-8") as fh:
    text = fh.read()

pattern = re.compile(
    r'^(?P<lead>[ \t]*' + re.escape(key) + r'[ \t]*=[ \t]*)"'
    + re.escape(placeholder) + r'"[ \t]*$',
    re.MULTILINE,
)
if not pattern.search(text):
    # Not present (already rotated, or line missing entirely). Emit empty
    # token so the caller knows nothing changed.
    print("")
    sys.exit(0)

token = secrets.token_urlsafe(32)
new_text, count = pattern.subn(lambda m: m.group("lead") + f'"{token}"', text)
if count == 0:
    print("")
    sys.exit(0)

# Atomic-ish rewrite: temp + rename, preserving the existing mode/owner.
tmp = cfg + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(new_text)
os.replace(tmp, cfg)
print(token)
PY
}

install_config() {
    local cfg="${CONFIG_DIR}/dualstream.toml"
    if [[ ! -f "${cfg}" ]]; then
        log "Installing default config to ${cfg}"
        install -o root -g root -m 0640 \
            "${REPO_ROOT}/config/dualstream.toml" \
            "${cfg}"
    else
        log "Existing ${cfg} preserved; rotating placeholder tokens and adding any missing keys"
    fi

    # Tighten ownership/mode so the token isn't world-readable. The
    # dualstream service user is in the dualstream group, so group-read is
    # sufficient.
    chown root:"${USER_NAME}" "${cfg}"
    chmod 0640 "${cfg}"

    # Older configs (from before viewer_token existed) won't have the line
    # at all — add it. Inserted just after the [server] header so it lives
    # in the right TOML section. Done in Python for portability.
    if ! grep -Eq '^[[:space:]]*viewer_token[[:space:]]*=' "${cfg}"; then
        CFG="${cfg}" python3 - <<'PY'
import os, re
cfg = os.environ["CFG"]
with open(cfg, "r", encoding="utf-8") as fh:
    text = fh.read()
insertion = 'viewer_token = "change-me-viewer"\n'
m = re.search(r'^[ \t]*\[server\][ \t]*\r?\n', text, re.MULTILINE)
if m:
    text = text[: m.end()] + insertion + text[m.end():]
else:
    if not text.endswith("\n"):
        text += "\n"
    text += "\n[server]\n" + insertion
tmp = cfg + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(text)
os.replace(tmp, cfg)
PY
        log "Added missing viewer_token line to ${cfg}"
    fi

    local admin_token viewer_token
    admin_token="$(rotate_placeholder_token "${cfg}" "auth_token" "change-me")"
    viewer_token="$(rotate_placeholder_token "${cfg}" "viewer_token" "change-me-viewer")"

    if [[ -n "${admin_token}" ]]; then
        log "Generated random admin auth_token in ${cfg}"
        ADMIN_TOKEN_GENERATED="${admin_token}"
    else
        log "Admin auth_token already customised; leaving as-is"
    fi
    if [[ -n "${viewer_token}" ]]; then
        log "Generated random viewer_token in ${cfg}"
        VIEWER_TOKEN_GENERATED="${viewer_token}"
    else
        log "viewer_token already customised; leaving as-is"
    fi
}

setup_venv() {
    # We deliberately do venv creation and pip install AS ROOT, not as the
    # dualstream system user. Rationale: the cloned repo usually sits under
    # /home/<some-user>/... which is mode 700/750 on Pi OS, so the dualstream
    # system user cannot read it. Running pip as the unprivileged user then
    # fails with a confusing "Invalid requirement / File does not exist"
    # because pip can't see the source tree. We chown the install prefix back
    # to dualstream at the end; file ownership only matters for writes, and
    # the venv binaries are world-readable/executable.
    if [[ ! -d "${INSTALL_PREFIX}/.venv" ]]; then
        log "Creating venv at ${INSTALL_PREFIX}/.venv (with --system-site-packages)"
        python3 -m venv --system-site-packages "${INSTALL_PREFIX}/.venv"
    else
        log "Reusing existing venv at ${INSTALL_PREFIX}/.venv"
    fi
    log "Upgrading pip"
    "${INSTALL_PREFIX}/.venv/bin/pip" install --upgrade pip
    log "Installing DualStream from ${REPO_ROOT}"
    "${INSTALL_PREFIX}/.venv/bin/pip" install "${REPO_ROOT}"
    log "Setting ownership of ${INSTALL_PREFIX} to ${USER_NAME}"
    chown -R "${USER_NAME}:${USER_NAME}" "${INSTALL_PREFIX}"
}

install_systemd_unit() {
    log "Installing systemd unit at ${SERVICE_FILE}"
    install -o root -g root -m 0644 "${REPO_ROOT}/scripts/dualstream.service" "${SERVICE_FILE}"
    systemctl daemon-reload
    systemctl enable dualstream.service
    log "Use 'systemctl start dualstream' to start the service"
    log "Use 'journalctl -u dualstream -f' to follow logs"
}

check_camera_overlays() {
    local config_txt="/boot/firmware/config.txt"
    if [[ ! -f "${config_txt}" ]]; then
        log "WARNING: ${config_txt} not found; cannot check CSI overlays"
        return
    fi
    local seen_cam0=0 seen_cam1=0
    if grep -Eq '^dtoverlay=imx708,cam0' "${config_txt}"; then seen_cam0=1; fi
    if grep -Eq '^dtoverlay=imx708,cam1' "${config_txt}"; then seen_cam1=1; fi
    if (( seen_cam0 && seen_cam1 )); then
        log "Both Pi Camera 3 dtoverlays already present in config.txt"
    else
        log "NOTE: did not find explicit dtoverlays for both Pi Camera 3 modules in ${config_txt}."
        log "If both cameras don't enumerate with 'rpicam-hello --list-cameras', add:"
        log "    camera_auto_detect=0"
        log "    dtoverlay=imx708,cam0"
        log "    dtoverlay=imx708,cam1"
        log "to the end of ${config_txt} and reboot."
    fi
}

print_token_summary() {
    if [[ -z "${ADMIN_TOKEN_GENERATED:-}" && -z "${VIEWER_TOKEN_GENERATED:-}" ]]; then
        return
    fi
    printf '\n'
    log "================================================================"
    log "AUTH TOKENS"
    log "These were freshly generated and written to ${CONFIG_DIR}/dualstream.toml."
    log "Save them somewhere safe; they are not displayed again."
    log "----------------------------------------------------------------"
    if [[ -n "${ADMIN_TOKEN_GENERATED:-}" ]]; then
        log "  admin auth_token (paste into the dual-tile UI at /):"
        log "      ${ADMIN_TOKEN_GENERATED}"
    fi
    if [[ -n "${VIEWER_TOKEN_GENERATED:-}" ]]; then
        log "  public viewer_token (use in shareable URLs):"
        log "      ${VIEWER_TOKEN_GENERATED}"
        log "  Shareable URLs (replace <host> with this Pi's tailnet name):"
        log "      http://<host>:8080/cam0?key=${VIEWER_TOKEN_GENERATED}"
        log "      http://<host>:8080/cam1?key=${VIEWER_TOKEN_GENERATED}"
    fi
    log "================================================================"
}

main() {
    require_root
    check_platform
    install_apt_deps
    install_tailscale
    create_user
    create_directories
    install_config
    setup_venv
    install_systemd_unit
    check_camera_overlays
    log "Install complete. Next steps:"
    log "  1. (If needed) update ${CONFIG_DIR}/dualstream.toml then reboot"
    log "  2. Run: sudo tailscale up   (if installed and not yet joined)"
    log "  3. Run: sudo systemctl start dualstream"
    log "  4. Browse to http://<this-host-tailnet-name>:8080/"
    print_token_summary
}

main "$@"
