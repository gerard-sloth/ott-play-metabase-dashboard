#!/bin/bash
# Hourly analytics ETL sync.
# Runs the full pipeline: user snapshots, chat events, daily stats, topic events.
#
# Usage (manual):
#   bash scripts/sync_analytics.sh
#
# Cron setup (every hour):
#   crontab -e
#   0 * * * * /bin/bash /ABSOLUTE/PATH/TO/ott-play-metabase-dashboard/scripts/sync_analytics.sh >> /ABSOLUTE/PATH/TO/ott-play-metabase-dashboard/logs/sync.log 2>&1
#
# GEval (daily at 03:00, separate — it makes LLM API calls):
#   0 3 * * * /bin/bash /ABSOLUTE/PATH/TO/ott-play-metabase-dashboard/scripts/run_geval_cron.sh >> /ABSOLUTE/PATH/TO/ott-play-metabase-dashboard/logs/geval.log 2>&1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=========================================="
echo "Analytics sync started: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

cd "$PROJECT_DIR"

# Load .env if present (cron doesn't inherit shell environment)
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    source "$PROJECT_DIR/.env"
    set +a
fi

uv run python -m src.analytics_sync

echo "Sync complete: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""
