# scripts

* `daily_update.sh` — the nightly pipeline: refresh the store, rebuild every derived layer in
  dependency order, then run each strategy in `config/strategies/`. Point cron or launchd at it after
  the close. See [operations](../docs/operations.md).
