# Architecture and design decisions

## Shape of the system

Each layer reads the one below and writes a curated table. Nothing skips a level, and every layer can
be rebuilt from the one beneath it.

```
vendors        Massive (bars, reference, corporate actions, news, short interest)
               SEC EDGAR (company facts)
                 |
ingestion      raw/  immutable vendor files + manifest.sqlite
                 |
identity       security_master, securities, security_attributes
                 |
prices         panel (daily, split and dividend aware)      intraday (from minute bars)
                 |
selection      universes (point in time, monthly rebalance)
                 |
features       price, size, fund, short, news, intra
                 |
evaluation     forward_returns -> IC, decay, quantiles, turnover
                 |
portfolio      risk model, sector neutrality, constraints, vol targeting
                 |
execution      paper broker, order generation, daily pipeline
```

## The decisions that matter

**Security identifiers, not tickers.** Tickers are reused and renamed. Every curated table is keyed by
`security_id`, derived from composite FIGI where available. Building this first cost a day and removed
an entire class of silent errors: `FB` alone would have quietly mixed Meta with an unrelated ETF.

**Point in time everywhere.** Universe membership uses lagged liquidity and is frozen between
rebalances. SEC values become usable on their filing date, not their period end. Reference attributes
come from monthly snapshots taken as of the date. News published after 16:00 counts toward the next
session. Short interest carries a ten-day publication lag. The test suite has a no-look-ahead check
that pumps volume on a rebalance date and asserts membership does not move.

**Unadjusted prices, adjusted returns.** Stored prices are as traded. Splits and dividends are stored
as events and applied when computing returns. This means the store never needs rewriting when a
corporate action is announced, and it makes the adjustment logic visible rather than baked in.

**Raw files are immutable.** Every vendor response is archived. Schema changes and parsing bugs are
recoverable without re-downloading 26 GB.

**Measurements separate from modelling choices.** The intraday table stores what the minute bars say.
The tick floor applied to the spread estimate is a modelling choice and lives in one documented
function. Where the two got mixed, as with an early attempt to derive a better closing price, the
result was worse than the vendor's own and was removed.

**Vendors behind an interface.** `DataProvider` has optional capabilities. Massive implements bars and
reference; SEC implements company facts. Adding a vendor does not touch research code.

## Things that were tried and rejected

**A derived official close.** The bar stamped at 16:00 seemed like the closing auction. It spans a full
minute and mixes the auction with early after-hours prints, so its close can be an after-hours price:
Intuit on 2026-08-25 shows 316.39 against an official 357.46. The vendor's daily close is authoritative.
The auction's first print survives as a diagnostic and matches the official close for 97% of liquid
names.

**Corwin-Schultz spreads on minute bars.** The estimator is inverted at minute frequency, reporting the
least liquid decile as the cheapest, because single-trade bars have no range and floor it at zero. It
stays in the code as a reference implementation and is documented as unusable. Abdi-Ranaldo, which
recovers a known 50 bp spread as 49.9 bp on simulated bars, is used instead.

**Mixing share-count concepts within an issuer.** Alibaba reports depositary shares on its cover page
and ordinary shares on its balance sheet. Taking whichever was most recent produced a $3.4 trillion
market cap. One concept per issuer, chosen by how often the issuer reports it.

## Known limitations

| area | limitation |
|---|---|
| data tier | no trades, quotes or vendor financial statements on Starter |
| spreads | estimated from bars, not quotes; validated only cross-sectionally |
| risk model | statistical PCA factors, no fundamental factor structure; predicted vol runs below realised for broad books |
| capacity | impact is optional and simple, a square root of ADV participation |
| execution | paper only, no live broker adapter |
| fundamentals | revenue resolves for about 78% of names; no analyst estimates or earnings surprise |
| validation | walk-forward windows exist but nothing fits parameters yet, so overfitting control is your discipline |

## Performance

Measured on ten cores with the full five-year store.

| step | time |
|---|---|
| `ca panel build` | 5 s |
| `ca universe build all` | 35 s |
| `ca intraday build` (full rebuild) | under 2 min |
| `ca features build all` | about 2 min |
| `ca eval forward` | 7 s |
| a five-year backtest of 1,500 names | 1.5 s |
| `ca master build` (first run, with vendor lookups) | 3 min |

Most work is Polars over partitioned Parquet with day-level parallelism. Nothing loads the 26 GB of
minute bars at once; the intraday build processes one session at a time.

## Testing

76 tests, all offline, no network or store required. They cover the parts where a silent error would
be expensive: credential parsing, corporate-action maths, the security master resolving symbol reuse
and renames, universe rules including a no-look-ahead check, forward returns across delistings, signal
metrics against a synthetic signal with known IC, cost accounting per name, position accounting through
a flip from long to short, and the spread estimators against a simulated spread.

The pattern worth keeping: when a number cannot be validated against an external source, validate it
against a simulation where the answer is known, then check the cross-section behaves sensibly.
