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

# ── Install unit ─────────────────────────────────────────────────────────────
mkdir -p "${UNIT_DIR}"
SRC="${SCRIPT_DIR}/skcomm.service"
DST="${UNIT_DIR}/skcomm.service"

sed "s|@@PYTHON3@@|${PYTHON3}|g" "$SRC" > "$DST"
echo "Installed: $DST"

systemctl --user daemon-reload

if [ "$ENABLE" -eq 1 ]; then
    systemctl --user enable skcomm.service
    echo "Enabled skcomm.service (starts on login)"
fi

if [ "$START" -eq 1 ]; then
    systemctl --user start skcomm.service
    sleep 2
    systemctl --user status skcomm.service --no-pager | tail -8
fi

echo ""
echo "SKComm daemon setup complete."
echo "  Start:   systemctl --user start skcomm"
echo "  Status:  systemctl --user status skcomm"
echo "  Logs:    journalctl --user -u skcomm -f"
echo "  API:     curl http://localhost:9384/api/v1/status"
