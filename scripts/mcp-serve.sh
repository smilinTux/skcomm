#!/usr/bin/env bash
# -------------------------------------------------------------------
# skcomm MCP server launcher (tool-agnostic)
#
# Works with: Cursor, Claude Desktop, Claude Code CLI, Windsurf,
#             Aider, Cline, or any MCP client that speaks stdio.
#
# The script auto-detects the Python virtualenv and launches the
# MCP server on stdio. No hardcoded paths required in client configs.
#
# Usage:
#   ./skcomm/scripts/mcp-serve.sh          (from repo root)
#   bash skcomm/scripts/mcp-serve.sh       (explicit bash)
# -------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKCOMM_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# --- Locate the virtualenv ---
# Priority: SKCOMM_VENV env var > first venv with mcp installed
# Candidates: skmemory/.venv (shared project venv) > skcomm/.venv > repo .venv
find_venv() {
    if [[ -n "${SKCOMM_VENV:-}" ]] && [[ -f "$SKCOMM_VENV/bin/python" ]]; then
        echo "$SKCOMM_VENV"
        return
    fi

    local candidates=(
        "$REPO_ROOT/skmemory/.venv"
        "$SKCOMM_DIR/.venv"
        "$REPO_ROOT/.venv"
    )

    for venv in "${candidates[@]}"; do
        if [[ -f "$venv/bin/python" ]]; then
            if "$venv/bin/python" -c "import mcp" 2>/dev/null; then
                echo "$venv"
                return
            fi
        fi
    done

    # Fallback: return first venv that exists (may need pip install mcp)
    for venv in "${candidates[@]}"; do
        if [[ -f "$venv/bin/python" ]]; then
            echo "$venv"
            return
        fi
    done

    return 1
}

VENV_DIR="$(find_venv)" || {
    echo "ERROR: No Python virtualenv found." >&2
    echo "Create one with: python -m venv skcomm/.venv && skcomm/.venv/bin/pip install -e skcomm/" >&2
    exit 1
}

PYTHON="$VENV_DIR/bin/python"

# --- Ensure skcomm is importable ---
export PYTHONPATH="${SKCOMM_DIR}/src${PYTHONPATH:+:$PYTHONPATH}"

# --- Launch MCP server on stdio ---
exec "$PYTHON" -m skcomm.mcp_server "$@"
