#!/bin/bash
# GEval evaluation sync — runs every 8 hours.
# Makes LLM API calls — do not run more frequently than this.
#
# Cron setup (every 8 hours: 00:00, 08:00, 16:00):
#   0 0,8,16 * * * /bin/bash /ABSOLUTE/PATH/TO/ott-play-metabase-dashboard/scripts/run_geval_cron.sh >> /ABSOLUTE/PATH/TO/ott-play-metabase-dashboard/logs/geval.log 2>&1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=========================================="
echo "GEval sync started: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

cd "$PROJECT_DIR"

if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    source "$PROJECT_DIR/.env"
    set +a
fi

uv run python scripts/run_geval.py

echo "GEval sync complete: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""
