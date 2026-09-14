#!/usr/bin/env bash
# Incremental refresh after the close. Schedule with cron/launchd, e.g. 22:30 ET on weekdays:
#   30 22 * * 1-5 /Users/yuriturygin/Documents/GitHub/collective-alpha/scripts/daily_update.sh
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run ca update --days-back 5
