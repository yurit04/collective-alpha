"""Conduit: the workflow. plan (dry run, saved) -> submit (checks, then orders) -> fills ->
reconcile. Reads use a read-only broker connection; only submit and cancel open a writable one."""

from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from ._broker import Broker, OrderRequest
from ._config import ConduitError, ConduitSettings, is_paper, load_settings, mask
from ._contracts import OK, ContractBook
from ._journal import Journal
from ._orders import OrderSheet, make_orders, positions_hash

log = logging.getLogger("transwarp_conduit")
NY = ZoneInfo("America/New_York")
OPEN_STATES = ("PendingSubmit", "ApiPending", "PreSubmitted", "Submitted", "PendingCancel")
MARKET_LOOKBACK_DAYS = 120       # calendar days of returns for ADV (21 days) and vol (63 days)


def _day(x: Any) -> date | None:
    if x is None or isinstance(x, date) and not isinstance(x, datetime):
        return x
    if isinstance(x, datetime):
        return x.date()
    return date.fromisoformat(str(x))


class Conduit:
    """broker: a Broker to use for everything (tests, SimBroker rehearsals); None connects to IB
    with the settings (read-only unless sending or cancelling orders)."""

    def __init__(self, settings: ConduitSettings | None = None, broker: Broker | None = None, store=None,
                 engine=None, costs=None, clock=None):
        self.s = settings or load_settings()
        self.root = self.s.data_root()
        self.journal = Journal(self.root)
        self.book = ContractBook(self.root, store, self.s.contract_max_age_days)
        self._broker, self._store, self._engine, self.costs = broker, store, engine, costs
        self._clock = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------ plumbing
    @property
    def store(self):
        if self._store is None:
            from massive_subspace_stream import Store

            self._store = Store()
        return self._store

    @property
    def engine(self):
        if self._engine is None:
            from unimatrix_zero import Engine

            self._engine = Engine(store=self.store)
        return self._engine

    @contextmanager
    def session(self, write: bool = False):
        if self._broker is not None:
            yield self._broker
            return
        from ._ib import IBBroker

        b = IBBroker(self.s, readonly=not write)
        try:
            yield b
        finally:
            b.close()

    def now(self) -> datetime:
        return self._clock()

    def today(self) -> date:
        return self.now().astimezone(NY).date()

    # ------------------------------------------------------------------ reads
    def status(self) -> dict:
        with self.session() as b:
            acct, a = b.account_id(), b.account()
            pos, orders = b.positions(), b.orders()
        mine = orders.filter(pl.col("order_ref").is_not_null() & pl.col("status").is_in(OPEN_STATES))
        sheets = sorted(p.name for p in self.journal.sheets.glob("*") if p.is_dir()) if self.journal.sheets.exists() else []
        return {"account": mask(acct), "paper": is_paper(acct), "port": self.s.port,
                "net_liquidation": a.get("net_liquidation"), "cash": a.get("cash"),
                "gross_position_value": a.get("gross_position_value"), "positions": pos.height,
                "open_conduit_orders": mine.height, "capital_setting": self.s.capital,
                "last_sheet": sheets[-1] if sheets else None, "data_root": str(self.root)}

    def _classify(self, positions: pl.DataFrame, target_ids: set[str]) -> tuple[pl.DataFrame, pl.DataFrame]:
        """Split positions into managed (security_id, con_id, shares) and unmanaged ones. A
        position is managed if it is a target or this package has ordered it before."""
        sid = self.book.by_con_id()
        traded = self.journal.traded_con_ids()
        p = positions.with_columns(pl.col("con_id").replace_strict(sid, default=None, return_dtype=pl.String)
                                   .alias("security_id"))
        managed = p.filter((pl.col("security_id").is_in(list(target_ids)) | pl.col("con_id").is_in(list(traded)))
                           & pl.col("security_id").is_not_null())
        return managed.select("security_id", "con_id", "shares"), p.join(managed.select("con_id"), on="con_id", how="anti")

    def market(self, ids: list[str], on: date) -> pl.DataFrame:
        """security_id, price (last raw close on or before `on`), adv, vol, price_date."""
        r = self.engine.returns(ids, on - timedelta(days=MARKET_LOOKBACK_DAYS), on)
        r = r.filter(pl.col("raw_close").is_not_null()).sort("date")
        return r.group_by("security_id").agg(pl.col("raw_close").last().alias("price"), pl.col("adv").last(),
                                             pl.col("vol").last(), pl.col("date").last().alias("price_date"))

    def holdings(self) -> pl.DataFrame:
        """Managed positions as weights of capital (security_id, weight): the current book for
        borg_queen.Optimizer.solve(w0=...)."""
        with self.session() as b:
            a, pos = b.account(), b.positions()
        cap = self.s.capital or a["net_liquidation"]
        managed, _ = self._classify(pos, set())
        if managed.is_empty():
            return pl.DataFrame(schema={"security_id": pl.String, "weight": pl.Float64})
        m = self.market(managed["security_id"].to_list(), self.today())
        return managed.join(m, on="security_id", how="left").select(
            "security_id", (pl.col("shares") * pl.col("price") / cap).alias("weight"))

    # ------------------------------------------------------------------ plan
    def plan(self, targets: pl.DataFrame, on: Any = None, capital: float | None = None, refresh_contracts: bool = False,
             drop_blocked: bool = False, check_borrow: bool = True) -> OrderSheet:
        """Dry run: target weights (security_id, weight; a date column, if any, must hold one
        date) -> an OrderSheet, saved under the data root. Nothing is sent."""
        if "date" in targets.columns and targets["date"].n_unique() > 1:
            raise ConduitError("targets hold several dates; pass one rebalance")
        on = _day(on) or self.today()
        targets = targets.filter(pl.col("weight").is_not_null() & (pl.col("weight") != 0))
        tids = set(targets["security_id"].to_list())
        with self.session() as b:
            acct, a, pos = b.account_id(), b.account(), b.positions()
            contracts = self.book.resolve(sorted(tids), on, b, refresh=refresh_contracts)
            managed, unmanaged = self._classify(pos, tids)
            extra = sorted(set(managed["security_id"].to_list()) - tids)
            if extra:
                contracts = pl.concat([contracts, self.book.resolve(extra, on, b)])
            cap = capital or self.s.capital or a["net_liquidation"]
            if not cap or cap <= 0:
                raise ConduitError("no capital: the account reports no net liquidation value")
            mkt = self.market(sorted(tids | set(extra)), on)
            short_ids = []
            if check_borrow:
                neg = targets.filter(pl.col("weight") < 0)["security_id"].to_list()
                short_ids = contracts.filter(pl.col("security_id").is_in(neg) & pl.col("status").is_in(OK)
                                             )["con_id"].drop_nulls().to_list()
            shortable = b.shortable(short_ids) if short_ids else {}
        orders, issues, warnings = make_orders(targets, managed, contracts, mkt, cap, self.s.limits, self.costs,
                                               shortable, drop_blocked)
        if cap > a["net_liquidation"] * 1.0001:
            warnings.append(f"capital {cap:,.0f} is above the account's net liquidation {a['net_liquidation']:,.0f}")
        stale = mkt.filter(pl.col("price_date") < mkt["price_date"].max())
        if stale.height:
            warnings.append(f"{stale.height} name(s) priced from an older close than {mkt['price_date'].max()}")
        if unmanaged.height:
            warnings.append(f"{unmanaged.height} position(s) not managed by this package are left alone")
        price_date = mkt["price_date"].max() if mkt.height else on
        ph = positions_hash(pos)
        key = f"{self.now().isoformat()}|{acct}|{ph}|{targets.sort('security_id').write_csv()}"
        sheet_id = f"{self.today():%Y%m%d}-{hashlib.sha256(key.encode()).hexdigest()[:6]}"
        sheet = OrderSheet(sheet_id=sheet_id, created_at=self.now(), price_date=price_date, account=acct, capital=cap,
                           net_liquidation=a["net_liquidation"], order_type=self.s.order_type, positions_hash=ph,
                           orders=orders, unmanaged=unmanaged.select("con_id", "symbol", "shares"), issues=issues,
                           warnings=warnings, status="blocked" if issues else "planned")
        sheet.save(self.journal.sheets)
        self.journal.event("plan", sheet_id=sheet_id, account=mask(acct), status=sheet.status,
                           orders=sheet.to_send.height, issues=issues)
        return sheet

    def sheet(self, sheet_id: str) -> OrderSheet:
        return OrderSheet.load(self.journal.sheets, sheet_id)

    # ------------------------------------------------------------------ submit
    def cutoff(self, day: date) -> datetime | None:
        """Last time to send market-on-close orders on `day` (None: not a trading day)."""
        s = self.store.sessions(day, day)
        if s.is_empty():
            return None
        close = s["market_close"][0]
        close = close.replace(tzinfo=UTC) if close.tzinfo is None else close
        return close - timedelta(minutes=self.s.cutoff_minutes)

    def submit(self, sheet_id: str, live: bool = False) -> pl.DataFrame:
        """Send a planned sheet's orders after every check passes; otherwise raise ConduitError
        and send nothing. Paper accounts only, unless live=True, settings.live and
        settings.account name this (non-paper) account."""
        sheet = self.sheet(sheet_id)
        if sheet.status != "planned":
            raise ConduitError(f"sheet {sheet_id} is {sheet.status}" + (": " + "; ".join(sheet.issues) if sheet.issues else ""))
        if sheet_id in set(self.journal.submitted()["sheet_id"].to_list()):
            raise ConduitError(f"sheet {sheet_id} was already submitted")
        today = self.today()
        if sheet.created_at.astimezone(NY).date() != today:
            raise ConduitError("the sheet was planned on another day; plan again")
        cut = self.cutoff(today)
        if cut is None:
            raise ConduitError(f"{today} is not a trading day")
        if self.s.order_type in ("MOC", "LOC") and self.now() >= cut:
            raise ConduitError(f"too late for market-on-close orders today (cutoff {cut.astimezone(NY):%H:%M} ET)")
        send = sheet.to_send
        if send.is_empty():
            raise ConduitError("nothing to send")
        with self.session(write=True) as b:
            acct = b.account_id()
            if acct != sheet.account:
                raise ConduitError(f"connected to {mask(acct)}, but the sheet is for {mask(sheet.account)}")
            if not is_paper(acct):
                if not live:
                    raise ConduitError(f"{mask(acct)} is a live account: pass live=True (CLI: --live)")
                if not self.s.live or self.s.account != acct:
                    raise ConduitError("live orders need settings live = true and account = this account id")
            if positions_hash(b.positions()) != sheet.positions_hash:
                raise ConduitError("positions changed since the sheet was planned; plan again")
            open_mine = b.orders().filter(pl.col("order_ref").is_not_null() & pl.col("status").is_in(OPEN_STATES))
            if open_mine.height:
                raise ConduitError(f"{open_mine.height} order(s) from an earlier sheet are still open; "
                                   "cancel them or wait for them to finish")
            reqs = [OrderRequest(con_id=int(r["con_id"]), symbol=r["ib_symbol"], primary_exchange=r["ib_exchange"],
                                 action=r["action"], quantity=abs(float(r["trade_shares"])),
                                 order_type=sheet.order_type, order_ref=sheet_id) for r in send.iter_rows(named=True)]
            # Recorded before anything is sent: if journaling the placed orders fails, the event
            # still shows that this sheet's orders (orderRef = sheet id) may be at the broker.
            self.journal.event("submitting", sheet_id=sheet_id, account=mask(acct), orders=len(reqs),
                               live=not is_paper(acct))
            placed = b.place(reqs)
        self.journal.record_orders(placed, sheet_id, acct)
        sheet.status = "submitted"
        sheet.save(self.journal.sheets)
        self.journal.event("submit", sheet_id=sheet_id, account=mask(acct), orders=placed.height, live=not is_paper(acct))
        return placed

    # ------------------------------------------------------------------ after
    def orders(self, sheet_id: str | None = None) -> pl.DataFrame:
        with self.session() as b:
            o = b.orders()
        return o.filter(pl.col("order_ref") == sheet_id) if sheet_id else o.filter(pl.col("order_ref").is_not_null())

    def sync_fills(self) -> pl.DataFrame:
        """Pull today's executions into the journal; returns all journaled fills."""
        with self.session() as b:
            new = self.journal.record_fills(b.executions())
        if new:
            self.journal.event("fills", new=new)
        return self.journal.fills()

    def reconcile(self, sheet_id: str) -> pl.DataFrame:
        """Per name: shares before, target, ordered, filled (journal), expected, actual (broker),
        diff = actual - expected (should be 0) and unfilled = target - actual for sent orders."""
        sheet = self.sheet(sheet_id)
        fills = self.sync_fills().filter(pl.col("order_ref") == sheet_id)
        with self.session() as b:
            pos = b.positions()
        f = fills.group_by("con_id").agg(
            pl.when(pl.col("side") == "BOT").then(pl.col("shares")).otherwise(-pl.col("shares")).sum().alias("filled"),
            (pl.col("shares") * pl.col("price")).sum().alias("__px"), pl.col("shares").sum().alias("__q"),
            pl.col("commission").sum().alias("commission"))
        o = (sheet.orders.select("security_id", "ticker", "con_id", "current_shares", "target_shares", "status",
                                 pl.when(pl.col("status") == "send").then(pl.col("trade_shares")).otherwise(0.0)
                                 .alias("ordered"), pl.col("price").alias("decision_price"))
             .join(f, on="con_id", how="left")
             .join(pos.select("con_id", pl.col("shares").alias("actual")), on="con_id", how="left")
             .with_columns(pl.col("filled").fill_null(0.0), pl.col("actual").fill_null(0.0),
                           (pl.col("__px") / pl.col("__q")).alias("avg_fill_price")))
        o = o.with_columns((pl.col("current_shares") + pl.col("filled")).alias("expected"))
        o = o.with_columns((pl.col("actual") - pl.col("expected")).alias("diff"),
                           pl.when(pl.col("status") == "send").then(pl.col("target_shares") - pl.col("actual"))
                           .otherwise(0.0).alias("unfilled"),
                           ((pl.col("avg_fill_price") / pl.col("decision_price") - 1) * 1e4
                            * pl.col("ordered").sign()).alias("slippage_bps"))
        out = o.select("security_id", "ticker", "con_id", "current_shares", "target_shares", "ordered", "filled",
                       "expected", "actual", "diff", "unfilled", "avg_fill_price", "decision_price", "slippage_bps",
                       "commission")
        self.journal.event("reconcile", sheet_id=sheet_id, mismatches=int((out["diff"] != 0).sum()),
                           unfilled=int((out["unfilled"] != 0).sum()))
        return out

    def cancel(self, sheet_id: str) -> int:
        """Cancel this sheet's open orders."""
        with self.session(write=True) as b:
            ids = b.orders().filter((pl.col("order_ref") == sheet_id) & pl.col("status").is_in(OPEN_STATES))["order_id"]
            n = b.cancel(ids.to_list()) if ids.len() else 0
        self.journal.event("cancel", sheet_id=sheet_id, orders=n)
        return n

    def cancel_all(self) -> None:
        """Kill switch: every open order in the account, from any client."""
        with self.session(write=True) as b:
            b.cancel_all()
        self.journal.event("cancel_all")

