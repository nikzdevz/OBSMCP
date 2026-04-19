#!/usr/bin/env bash
set -euo pipefail

CONFIG="$HOME/.obsmcp/config.json"

if [[ ! -f "$CONFIG" ]]; then
    echo "================================================"
    echo "  OBSMCP First-Run Setup"
    echo "================================================"
    echo
    read -r -p "Enter project path: " PROJECT_PATH
    echo
    echo "Optional: cloud sync configuration (leave blank for standalone mode)"
    echo
    read -r -p "Enter backend URL (blank = standalone): " BACKEND_URL
    read -r -p "Enter API token (blank = no auth): " API_TOKEN
    echo

    python -m obsmcp.obsmcp_setup --configure \
        --project "$PROJECT_PATH" \
        --url "$BACKEND_URL" \
        --token "$API_TOKEN"

    if [[ -z "$BACKEND_URL" ]]; then
        echo "Mode: STANDALONE (all data stored locally)"
    else
        echo "Mode: CLOUD SYNC (data syncing to $BACKEND_URL)"
    fi
    echo
fi

exec python -m obsmcp "$@"
