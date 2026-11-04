# unicomplex (core, depends on massive-subspace-stream, edgar-cortical-node, finra-sensor-array, vinculum, borg-queen, transwarp-conduit)

Orchestration. Phase 1: `collective status`, a read-only freshness report over every store.
User docs: README.md in this folder. Design: docs/decisions/0012-unicomplex.md.

## Public API (`from unicomplex import ...`)

| Name | Use |
|---|---|
| `collect(massive=None, edgar=None, finra=None, vinculum=None, risk=None, conduit_root=None, now=None)` | every check as `list[Check]`; stores default to each package's configured one |
| `Check` | source, name, state (ok / stale / info / missing), last, expected, detail; `.to_dict()` |
| `render(checks, now=None)` | the table with a verdict line |
| `STATES` | the four states |

CLI: `collective status [--json]` (exit 1 when anything is stale or missing).

## Internals (private)

```
_checks.py   one function per source (massive_checks, edgar_checks, finra_checks, vinculum_checks,
             risk_checks, conduit_checks), the expectations, collect(), render(), to_json()
cli.py       `collective`
```

Invariants:
- Read-only: this package never downloads, builds, connects to IB or writes under any data root.
- Only public APIs of the other packages (`Store.coverage/stream_status/trading_days`,
  `EdgarStore.status`, `FinraStore.status`, `Vinculum.status`, `RiskModel.status`,
  `transwarp_conduit.Journal/OrderSheet/load_settings`): a status field another package adds
  or renames is picked up here, not reimplemented.
- One failing store reports `missing` with its error; the other checks still run.
- Expectations follow the schedules documented in each package (sync times, publication lags);
  change them here when a schedule changes there.
