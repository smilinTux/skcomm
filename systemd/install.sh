#!/usr/bin/env bash
# install.sh — Install skcomm systemd user unit
#
# Usage:
#   ./systemd/install.sh [--start] [--no-enable]
#
# Detects the correct Python 3 interpreter (pyenv or system) and
# substitutes it into the service template, then installs to
# ~/.config/systemd/user/skcomm.service

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
START=0
ENABLE=1

for arg in "$@"; do
    case "$arg" in
        --start)    START=1 ;;
        --no-enable) ENABLE=0 ;;
    esac
done

# ── Detect Python 3 ──────────────────────────────────────────────────────────
# Prefer the Python that has skcomm importable.
PYTHON3=""
for candidate in \
    "$(command -v python3 2>/dev/null)" \
    "${HOME}/.pyenv/versions/$(pyenv version-name 2>/dev/null || true)/bin/python3" \
    "/usr/bin/python3"
do
    if [ -n "$candidate" ] && [ -x "$candidate" ] 2>/dev/null; then
        if "$candidate" -c "import skcomm" 2>/dev/null; then
            PYTHON3="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON3" ]; then
    echo "ERROR: Could not find a Python 3 interpreter with skcomm installed."
    echo "Install skcomm first: pip install -e skcomm/"
    exit 1
fi

echo "Using Python: $PYTHON3"

# ── Install units ────────────────────────────────────────────────────────────
mkdir -p "${UNIT_DIR}"

# API service (FastAPI)
SRC="${SCRIPT_DIR}/skcomm.service"
DST="${UNIT_DIR}/skcomm.service"
sed "s|@@PYTHON3@@|${PYTHON3}|g" "$SRC" > "$DST"
echo "Installed: $DST"

# Receive daemon (multi-agent poller)
SRC_DAEMON="${SCRIPT_DIR}/skcomm-daemon.service"
DST_DAEMON="${UNIT_DIR}/skcomm-daemon.service"
if [ -f "$SRC_DAEMON" ]; then
    cp "$SRC_DAEMON" "$DST_DAEMON"
    echo "Installed: $DST_DAEMON"
fi

if [[ "$(uname)" == "Darwin" ]]; then
    echo ""
    echo "NOTE: systemd is not available on macOS."
    echo "Unit files have been written to ${UNIT_DIR} for reference,"
    echo "but you will need to run SKComm manually or create a launchd plist."
    echo ""
    echo "  Manual start:"
    echo "    ${PYTHON3} -m skcomm serve"
    echo ""
    echo "  Or create a launchd plist at:"
    echo "    ~/Library/LaunchAgents/io.skworld.skcomm.plist"
    echo ""
else
    systemctl --user daemon-reload

    if [ "$ENABLE" -eq 1 ]; then
        systemctl --user enable skcomm.service
        echo "Enabled skcomm.service (starts on login)"
        if [ -f "$DST_DAEMON" ]; then
            systemctl --user enable skcomm-daemon.service
            echo "Enabled skcomm-daemon.service (starts on login)"
        fi
    fi

    if [ "$START" -eq 1 ]; then
        systemctl --user start skcomm.service
        sleep 2
        systemctl --user status skcomm.service --no-pager | tail -8
        if [ -f "$DST_DAEMON" ]; then
            systemctl --user start skcomm-daemon.service
            sleep 1
            systemctl --user status skcomm-daemon.service --no-pager | tail -8
        fi
    fi

    echo ""
    echo "SKComm setup complete."
    echo ""
    echo "  API service:"
    echo "    Start:   systemctl --user start skcomm"
    echo "    Status:  systemctl --user status skcomm"
    echo "    Logs:    journalctl --user -u skcomm -f"
    echo "    API:     curl http://localhost:9384/api/v1/status"
    echo ""
    echo "  Receive daemon (multi-agent):"
    echo "    Start:   systemctl --user start skcomm-daemon"
    echo "    Status:  systemctl --user status skcomm-daemon"
    echo "    Logs:    journalctl --user -u skcomm-daemon -f"
fi
