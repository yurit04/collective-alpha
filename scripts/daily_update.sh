#!/usr/bin/env bash
# Nightly pipeline after the close: refresh the store, rebuild derived tables, run paper strategies.
# Schedule with cron/launchd, e.g. 22:30 ET on weekdays:
#   30 22 * * 1-5 /Users/yuriturygin/Documents/GitHub/collective-alpha/scripts/daily_update.sh
set -euo pipefail
cd "$(dirname "$0")/.."
uv run ca update --days-back 5
uv run ca master build
uv run ca universe build all
uv run ca panel build
uv run ca features build all
uv run ca eval forward
for s in config/strategies/*.toml; do
  uv run ca trade run "$(basename "$s" .toml)"
done
