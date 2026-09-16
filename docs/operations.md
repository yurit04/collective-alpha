# Operations

## The nightly refresh

`scripts/daily_update.sh` chains the whole pipeline in dependency order:

```
ca update --days-back 5      # new sessions of bars, reference, corporate actions, monthly tables
ca master build              # security master, cheap after the first run
ca universe build all
ca panel build
ca intraday build            # incremental: only sessions without a file
ca features build all
ca eval forward
ca trade run <each strategy in config/strategies/>
```

Schedule it after the close, allowing time for the vendor to publish the session:

```cron
30 22 * * 1-5 /Users/yuriturygin/Documents/GitHub/collective-alpha/scripts/daily_update.sh >> ~/ca-nightly.log 2>&1
```

The script has not been run end to end unattended. Before trusting it, run it once by hand on a
weekday evening and read the log.

Every step is idempotent, so a failed run can simply be run again. `ca update` re-fetches only what is
missing or changed, `ca intraday build` skips sessions that already have a file, and the rest rebuild
from what is on disk.

## Checking the store is healthy

```bash
ca status                    # per dataset: files, downloaded, converted, errors, disk free
ca status --errors           # the actual error text
ca coverage day_aggs_v1      # trading days with no file
ca intraday check            # session coverage against the exchange calendar
ca panel check               # return sanity and the largest moves
```

Things that should make you stop and look: any non-zero error count, a missing day that is not an
exchange holiday, or `panel check` reporting extreme returns in liquid names.

## Paper trading

A strategy is a TOML file in `config/strategies/`:

```toml
name = "momentum_ls"
universe = "liquid_1500"
rebalance = "monthly"
capital = 100000
broker = "paper"
min_notional = 200

[signals]              # feature = weight; several are z-scored and combined
mom_12_1 = 1.0

[portfolio]
method = "heuristic"
gross = 1.6
net = 0.0
max_weight = 0.03
n_names = 100          # a small book cannot hold 1,500 names above the minimum trade size
sector_neutral = true
vol_target = 0.10
```

Each session the pipeline fills orders left pending from the previous run at today's open, marks the
book at the close, and on a rebalance day computes targets and submits orders that will execute at the
next open.

```bash
ca trade run momentum_ls                    # one session, defaults to the last completed one
ca trade replay momentum_ls --start 2026-06-01 --end 2026-09-11   # seed or audit a book
ca trade status momentum_ls                 # cash, positions, pending orders, NAV
ca trade export momentum_ls --path orders.csv
```

State lives in `<data_root>/paper/<strategy>.sqlite`: positions, cash, every order and fill, and a
daily NAV series. Delete that file to start over.

**There is no live broker adapter.** `broker = "paper"` is the only accepted value. `ca trade export`
writes a CSV of pending orders with ticker, side, quantity and reference price, which is the bridge to
a real account.

## Sizing a book

A common surprise: at a 10% volatility target a concentrated momentum book runs at about a third of
its gross target, so a $100 k account ends up with $300 positions. Either drop the volatility target,
raise capital, or lower `n_names`. Check what you are actually getting with `ca trade status` and the
exposure report from `ca portfolio feature`.

## Troubleshooting

| symptom | cause and fix |
|---|---|
| `ca probe` shows 403 on trades, quotes, financials | expected on the Starter plan, those datasets are skipped |
| SEC sync returns HTTP 403 | set `sec_user_agent` to a string containing a contact email |
| `feature group X not built` | run `ca features build X` |
| `run ca master build first` | the security master is the base of every keyed table |
| a rebuilt table has mixed schemas | rebuild the whole table with `--force`, not a date subset |
| backtest results identical with and without a flag | confirm the flag reaches the engine before trusting either number |
| minute-bar work classifies everything as pre-market | time-of-day arithmetic overflowed an 8-bit hour; cast before multiplying |

## Backups

`raw/` can always be re-downloaded, slowly. `curated/` can always be rebuilt from `raw/`. The only
irreplaceable state is `<data_root>/paper/*.sqlite`, your trading history, and `config/settings.toml`.
Back those up.
