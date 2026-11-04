"""Interactive Brokers adapter (ib_async against IB Gateway or TWS). The only module that
imports ib_async. Read-only connections cannot place or cancel orders (IB enforces it too)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import polars as pl

from ._broker import CONTRACTS, FILLS, ORDERS, POSITIONS, OrderRequest, empty
from ._config import ConduitError, ConduitSettings

log = logging.getLogger("transwarp_conduit")
MAX_LINES = 50
SUMMARY_TAGS = {"NetLiquidation": "net_liquidation", "TotalCashValue": "cash",
                "GrossPositionValue": "gross_position_value"}


def _allow_nested_loop(ib_async) -> None:
    """ib_async drives its own asyncio loop synchronously. Inside a Jupyter kernel a loop is
    already running in this thread, so connecting would fail with "This event loop is already
    running"; ib_async's `util.startLoop()` (nest_asyncio) makes the two coexist."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    ib_async.util.startLoop()


class IBBroker:
    def __init__(self, settings: ConduitSettings, readonly: bool = True):
        import ib_async

        logging.getLogger("ib_async").setLevel(logging.WARNING)
        self.s, self.readonly, self._ib_mod = settings, readonly, ib_async
        _allow_nested_loop(ib_async)
        self.ib = ib_async.IB()
        try:
            self.ib.connect(settings.host, settings.port, clientId=settings.client_id, timeout=settings.timeout,
                            readonly=readonly, account=settings.account or "")
        except Exception as exc:  # ConnectionRefusedError, TimeoutError, API errors
            raise ConduitError(f"cannot connect to IB at {settings.host}:{settings.port} ({type(exc).__name__}: {exc}). "
                               "Is IB Gateway or TWS running and logged in, with the API enabled on this port?") from exc
        accounts = self.ib.managedAccounts()
        if not accounts:
            self.close()
            raise ConduitError("IB returned no accounts")
        if settings.account and settings.account not in accounts:
            self.close()
            raise ConduitError("the configured account is not one this login manages")
        if not settings.account and len(accounts) > 1:
            self.close()
            raise ConduitError(f"this login manages {len(accounts)} accounts; set transwarp_conduit.account")
        self._account = settings.account or accounts[0]

    # ------------------------------------------------------------------ reads
    def account_id(self) -> str:
        return self._account

    def account(self) -> dict:
        out = {"account": self._account, "currency": "USD"}
        for v in self.ib.accountSummary(self._account):
            usable = v.tag in SUMMARY_TAGS and v.currency in ("USD", "BASE", "")
            if usable and (v.currency == "USD" or SUMMARY_TAGS[v.tag] not in out):   # USD wins over BASE
                out[SUMMARY_TAGS[v.tag]] = float(v.value)
        return out

    def positions(self) -> pl.DataFrame:
        rows = [{"con_id": p.contract.conId, "symbol": p.contract.symbol, "shares": float(p.position),
                 "avg_cost": float(p.avgCost)} for p in self.ib.positions(self._account)
                if p.contract.secType == "STK" and p.position]
        return pl.DataFrame(rows, schema=POSITIONS) if rows else empty(POSITIONS)

    def contract_details(self, symbol: str) -> pl.DataFrame:
        return self._details(self._ib_mod.Stock(symbol, "SMART", "USD"), symbol)

    def contract_by_figi(self, figi: str) -> pl.DataFrame:
        """Empty when IB does not know the FIGI (IB answers with error 200, which is expected
        here and kept out of the log)."""
        c = self._ib_mod.Contract(secIdType="FIGI", secId=figi, secType="STK", exchange="SMART", currency="USD")
        quiet = logging.getLogger("ib_async.wrapper")
        level = quiet.level
        quiet.setLevel(logging.CRITICAL)
        try:
            return self._details(c, figi)
        finally:
            quiet.setLevel(level)

    def _details(self, c, label: str) -> pl.DataFrame:
        try:
            details = self.ib.reqContractDetails(c)
        except Exception as exc:
            log.debug("contract details %s: %s", label, exc)
            details = []
        rows = [{"con_id": d.contract.conId, "symbol": d.contract.symbol, "sec_type": d.contract.secType,
                 "currency": d.contract.currency, "primary_exchange": d.contract.primaryExchange,
                 "long_name": d.longName, "stock_type": d.stockType} for d in details]
        return pl.DataFrame(rows, schema=CONTRACTS) if rows else empty(CONTRACTS)

    def shortable(self, con_ids: list[int]) -> dict[int, float | None]:
        """Shares available to borrow (generic tick 236). Asks for delayed data, which IB serves
        without a market data subscription and which carries the shortable count; a name with no
        answer is None (unknown), never a guess."""
        out: dict[int, float | None] = {}
        self.ib.reqMarketDataType(3)                         # live if subscribed, else delayed
        quiet = logging.getLogger("ib_async.wrapper")
        level = quiet.level
        quiet.setLevel(logging.CRITICAL)                     # "needs a subscription; delayed available"
        try:
            self._shortable(con_ids, out)
        finally:
            quiet.setLevel(level)
        return out

    def _shortable(self, con_ids: list[int], out: dict) -> None:
        for i in range(0, len(con_ids), MAX_LINES):          # IB allows about 100 data lines at once
            tickers = []
            for cid in con_ids[i:i + MAX_LINES]:
                c = self._ib_mod.Contract(conId=cid, exchange="SMART", currency="USD", secType="STK")
                tickers.append((cid, c, self.ib.reqMktData(c, "236", False, False)))
            for _ in range(16):                                  # until every name answered, at most 8 s
                self.ib.sleep(0.5)
                if all(t.shortableShares is not None and t.shortableShares == t.shortableShares for _, _, t in tickers):
                    break
            for cid, c, t in tickers:
                v = t.shortableShares
                out[cid] = float(v) if v is not None and v == v else None
                self.ib.cancelMktData(c)

    def _order_row(self, t) -> dict:
        st = t.orderStatus
        return {"order_id": t.order.orderId, "perm_id": t.order.permId or st.permId, "con_id": t.contract.conId,
                "symbol": t.contract.symbol, "action": t.order.action, "quantity": float(t.order.totalQuantity),
                "order_type": t.order.orderType, "order_ref": t.order.orderRef or None, "status": st.status,
                "filled": float(st.filled or 0), "remaining": float(st.remaining or 0),
                "avg_fill_price": float(st.avgFillPrice) if st.avgFillPrice else None}

    def orders(self) -> pl.DataFrame:
        trades = list(self.ib.reqAllOpenOrders()) + list(self.ib.reqCompletedOrders(apiOnly=True))
        rows = [self._order_row(t) for t in trades if not t.order.account or t.order.account == self._account]
        return pl.DataFrame(rows, schema=ORDERS).unique(["perm_id", "status"], keep="last") if rows else empty(ORDERS)

    def executions(self) -> pl.DataFrame:
        fills = self.ib.reqExecutions(self._ib_mod.ExecutionFilter(acctCode=self._account))
        self.ib.sleep(0.5)                                         # commission reports follow executions
        rows = []
        for f in fills:
            e, cr = f.execution, f.commissionReport
            t = f.time if f.time.tzinfo else f.time.replace(tzinfo=UTC)
            rows.append({"exec_id": e.execId, "time": t, "account": e.acctNumber, "con_id": f.contract.conId,
                         "symbol": f.contract.symbol, "side": e.side, "shares": float(e.shares),
                         "price": float(e.price), "commission": float(cr.commission) if cr and cr.commission else None,
                         "order_id": e.orderId, "perm_id": e.permId, "order_ref": e.orderRef or None})
        return pl.DataFrame(rows, schema=FILLS) if rows else empty(FILLS)

    # ------------------------------------------------------------------ writes
    def _writable(self) -> None:
        if self.readonly:
            raise ConduitError("read-only connection: open the broker with readonly=False to send orders")

    def place(self, orders: list[OrderRequest]) -> pl.DataFrame:
        self._writable()
        m = self._ib_mod
        trades = []
        for o in orders:
            c = m.Contract(conId=o.con_id, symbol=o.symbol, secType="STK", exchange="SMART", currency="USD",
                           primaryExchange=o.primary_exchange or "")
            order = m.Order(action=o.action, totalQuantity=o.quantity, orderType=o.order_type, tif="DAY",
                            account=self._account, orderRef=o.order_ref, transmit=True)
            if o.limit_price is not None:
                order.lmtPrice = o.limit_price
            trades.append(self.ib.placeOrder(c, order))
        self.ib.sleep(1.0)
        rows = [self._order_row(t) for t in trades]
        return pl.DataFrame(rows, schema=ORDERS) if rows else empty(ORDERS)

    def cancel(self, order_ids: list[int]) -> int:
        self._writable()
        want, n = set(order_ids), 0
        for t in self.ib.openTrades():
            if t.order.orderId in want:
                self.ib.cancelOrder(t.order)
                n += 1
        self.ib.sleep(1.0)
        return n

    def cancel_all(self) -> None:
        """Every open order in the account, from any client (IB's global cancel)."""
        self._writable()
        self.ib.reqGlobalCancel()
        self.ib.sleep(1.0)

    def close(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)
