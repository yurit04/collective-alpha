# unicomplex

Orchestration for the collective. Phase 1 is one read-only command:

```bash
uv run collective status          # how fresh every store is against the nightly schedule
uv run collective status --json   # the same as JSON (one object per check)
```

It exits 1 when anything is stale or missing, so a cron job, `tools/nightly_sync.sh` or a
shell prompt can act on it. Nothing here downloads, builds, connects to a broker or trades.

## What it checks

| Source | Check | Fresh when |
|---|---|---|
| massive | `bars_day`, `bars_minute` | the last stored day is the last completed NYSE session before today (the 06:30 sync) |
| massive | `stream` | the stream service is running and, in market hours, has received a message in the last 30 minutes |
| edgar | `facts` | `sync_through` is within 3 days of today (the sync runs Tue-Sat through yesterday) |
| edgar | `series` | the materialized series were built from the current facts (`cortical series`) |
| finra | `short_interest` | the last settlement date is within 26 days (two cycles plus the 7-business-day publication lag) |
| finra | `short_volume` | the last day is the last completed session |
| vinculum | `market_cap` | the panel runs through the last completed session (`vinculum build` after the syncs) |
| borg-queen | `risk_model` | the last month end in the model is within 45 days (it is rebuilt by hand) |
| transwarp-conduit | `orders` | informational: the last order sheet and its status, fills journaled (read from the journal, no Gateway connection) |

States: `ok`, `stale` (behind the schedule), `info` (no expectation), `missing` (the store is
absent or unreadable; the error is shown, the other checks still run).

```python
from unicomplex import collect, render
checks = collect()                 # list[Check]; pass store objects or now= for tests
print(render(checks))
```

## Later phases
Scheduled runs with one log and one lock (replacing the per-package launchd jobs and
`tools/nightly_sync.sh`), and rebalance-day orchestration for transwarp-conduit.
