# transwarp-conduit (core, depends on unimatrix-zero, massive-subspace-stream)

Execution through Interactive Brokers (ib_async against IB Gateway/TWS). Phase 1: account,
contract mapping, order sheets with limits, paper MOC orders, journal, reconciliation.
User docs: README.md in this folder. Design: docs/decisions/0010-transwarp-conduit.md.

## Public API (`from transwarp_conduit import ...`)

| Name | Use |
|---|---|
| `Conduit(settings=None, broker=None, store=None, engine=None, costs=None, clock=None)` | the workflow; see below |
| `load_settings(config_file=None, **overrides)`, `ConduitSettings`, `Limits` | `[transwarp_conduit]` (+ `.limits`) in collective.toml, `TRANSWARP_*` env |
| `Broker` (Protocol), `OrderRequest`, `SimBroker` | broker interface (`contract_by_figi`, `contract_details`, `shortable`, `place`, ...); in-memory broker for tests and rehearsals (`.figis`, `.fill(prices)`) |
| `OrderSheet` | `.orders`, `.to_send`, `.unmanaged`, `.issues`, `.warnings`, `.status`, `.summary()`, `.save/.load` |
| `make_orders(targets, held, contracts, market, capital, limits, costs, shortable, drop_blocked)` | pure order rules -> (orders, issues, warnings) |
| `ContractBook(root, store, max_age_days)` | `.resolve(ids, on, broker, refresh)`, `.by_con_id()`, `.overrides()` |
| `Journal(root)` | sheets, `.submitted()`, `.fills()`, `.events()` |
| `names_match`, `ib_symbol`, `is_paper`, `positions_hash`, `ConduitError`, `ENV_ROOT` | helpers |

`Conduit` methods: `status()`, `holdings()` (managed positions as weights, for Optimizer w0),
`closed_through()` (the newest day whose close has happened), `market(ids, on)` (prices on or
before `on`, capped at `closed_through()`), `plan(targets, on, capital, refresh_contracts, drop_blocked, check_borrow)` ->
OrderSheet (saved, nothing sent), `sheet(id)`, `cutoff(day)`, `submit(id, live=False)`,
`orders(id)`, `sync_fills()`, `reconcile(id)`, `cancel(id)`, `cancel_all()`, `session(write)`.

CLI: `conduit status | contracts | plan | show | submit | orders | fills | reconcile | cancel | cancel-all`.

## Internals (private)

```
_config.py     ConduitSettings, Limits, load_settings, is_paper, mask
_broker.py     Broker protocol, frame schemas, OrderRequest, SimBroker
_ib.py         IBBroker: the only ib_async import; read-only unless readonly=False
_contracts.py  ContractBook: FIGI lookup first, else symbol + name/exchange checks; cache, overrides
_orders.py     make_orders (whole shares, skips, per-order/per-sheet checks), OrderSheet
_journal.py    append-only sheets / orders / fills / events
_conduit.py    Conduit workflow and submit checks
cli.py         `conduit`
```

Invariants (safety first; never weaken without the user's say-so):
- Nothing is sent outside `Conduit.submit`, and submit sends nothing unless every check passes
  (planned, same day, before the MOC cutoff, same account, unchanged positions, no open orders
  from earlier sheets, paper unless live=True + settings.live + settings.account match).
- Prices never come from a close that has not happened at `now()` (`market` caps `on` at
  `closed_through()`), whatever `on` or the clock says.
- Reads open read-only IB connections; only submit/cancel/cancel_all open writable ones.
- Never trade a name whose contract status is not ok/override; never sell a position this
  package did not order (unmanaged positions are only reported).
- No credentials anywhere: IB Gateway holds the login. Print account ids masked (`mask`).
- The journal is append-only; never delete or rewrite it.
- Tests run offline with SimBroker; anything that needs a running Gateway is `@pytest.mark.live`
  and read-only.
