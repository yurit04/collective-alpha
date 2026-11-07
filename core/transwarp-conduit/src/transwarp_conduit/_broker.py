"""The broker interface every adapter implements, its frame schemas, and SimBroker (in memory,
for tests and rehearsals)."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

import polars as pl

POSITIONS = {"con_id": pl.Int64, "symbol": pl.String, "shares": pl.Float64, "avg_cost": pl.Float64}
CONTRACTS = {"con_id": pl.Int64, "symbol": pl.String, "sec_type": pl.String, "currency": pl.String,
             "primary_exchange": pl.String, "long_name": pl.String, "stock_type": pl.String}
ORDERS = {"order_id": pl.Int64, "perm_id": pl.Int64, "con_id": pl.Int64, "symbol": pl.String,
          "action": pl.String, "quantity": pl.Float64, "order_type": pl.String, "order_ref": pl.String,
          "status": pl.String, "filled": pl.Float64, "remaining": pl.Float64, "avg_fill_price": pl.Float64}
FILLS = {"exec_id": pl.String, "time": pl.Datetime("us", "UTC"), "account": pl.String, "con_id": pl.Int64,
         "symbol": pl.String, "side": pl.String, "shares": pl.Float64, "price": pl.Float64,
         "commission": pl.Float64, "order_id": pl.Int64, "perm_id": pl.Int64, "order_ref": pl.String}


def empty(schema: dict) -> pl.DataFrame:
    return pl.DataFrame(schema=schema)


@dataclass(frozen=True)
class OrderRequest:
    con_id: int
    symbol: str
    primary_exchange: str | None
    action: str                 # BUY / SELL
    quantity: float             # positive whole shares
    order_type: str             # MOC or MKT (ORDER_TYPES)
    order_ref: str              # the order sheet id: links orders and fills to it
    limit_price: float | None = None


@runtime_checkable
class Broker(Protocol):
    """What Conduit needs from a broker. All frames follow the schemas above."""

    def account_id(self) -> str: ...
    def account(self) -> dict: ...                       # net_liquidation, cash, gross_position_value, currency
    def positions(self) -> pl.DataFrame: ...             # POSITIONS
    def contract_details(self, symbol: str) -> pl.DataFrame: ...   # CONTRACTS: US stock matches for a symbol
    def contract_by_figi(self, figi: str) -> pl.DataFrame: ...     # CONTRACTS: matches for a FIGI (may be empty)
    def shortable(self, con_ids: list[int]) -> dict[int, float | None]: ...   # shares available to short
    def place(self, orders: list[OrderRequest]) -> pl.DataFrame: ...          # ORDERS
    def orders(self) -> pl.DataFrame: ...                # ORDERS: open and today's completed
    def executions(self) -> pl.DataFrame: ...            # FILLS: today's (IB keeps about a day)
    def cancel(self, order_ids: list[int]) -> int: ...   # open orders by id; returns how many
    def cancel_all(self) -> None: ...                    # every open order in the account
    def close(self) -> None: ...


@dataclass
class SimBroker:
    """In-memory broker. `contracts` maps a symbol to rows of contract details; `fill(prices)`
    fills every open order at the given prices (as the closing auction would)."""

    account_no: str = "DU0000001"
    cash: float = 1_000_000.0
    contracts: dict[str, list[dict]] = field(default_factory=dict)
    figis: dict[str, str] = field(default_factory=dict)          # FIGI -> symbol in `contracts`
    holdings: dict[int, float] = field(default_factory=dict)
    marks: dict[int, float] = field(default_factory=dict)        # con_id -> last price (for NAV)
    short_available: dict[int, float | None] = field(default_factory=dict)
    commission_per_share: float = 0.0035
    _orders: list[dict] = field(default_factory=list)
    _fills: list[dict] = field(default_factory=list)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    def _symbol(self, con_id: int) -> str:
        for sym, rows in self.contracts.items():
            if any(r["con_id"] == con_id for r in rows):
                return sym
        return str(con_id)

    def account_id(self) -> str:
        return self.account_no

    def account(self) -> dict:
        value = sum(q * self.marks.get(c, 0.0) for c, q in self.holdings.items())
        gross = sum(abs(q) * self.marks.get(c, 0.0) for c, q in self.holdings.items())
        return {"account": self.account_no, "net_liquidation": self.cash + value, "cash": self.cash,
                "gross_position_value": gross, "currency": "USD"}

    def positions(self) -> pl.DataFrame:
        rows = [{"con_id": c, "symbol": self._symbol(c), "shares": float(q), "avg_cost": self.marks.get(c)}
                for c, q in self.holdings.items() if q]
        return pl.DataFrame(rows, schema=POSITIONS) if rows else empty(POSITIONS)

    def _details(self, symbol: str | None) -> pl.DataFrame:
        rows = [{"symbol": symbol, "sec_type": "STK", "currency": "USD", "stock_type": "COMMON", **r}
                for r in self.contracts.get(symbol, [])] if symbol else []
        return pl.DataFrame(rows, schema=CONTRACTS) if rows else empty(CONTRACTS)

    def contract_details(self, symbol: str) -> pl.DataFrame:
        return self._details(symbol)

    def contract_by_figi(self, figi: str) -> pl.DataFrame:
        return self._details(self.figis.get(figi))

    def shortable(self, con_ids: list[int]) -> dict[int, float | None]:
        return {c: self.short_available.get(c) for c in con_ids}

    def place(self, orders: list[OrderRequest]) -> pl.DataFrame:
        out = []
        for o in orders:
            oid = next(self._ids)
            row = {"order_id": oid, "perm_id": 1000 + oid, "con_id": o.con_id, "symbol": o.symbol, "action": o.action,
                   "quantity": float(o.quantity), "order_type": o.order_type, "order_ref": o.order_ref,
                   "status": "Submitted", "filled": 0.0, "remaining": float(o.quantity), "avg_fill_price": None}
            self._orders.append(row)
            out.append(dict(row))
        return pl.DataFrame(out, schema=ORDERS) if out else empty(ORDERS)

    def orders(self) -> pl.DataFrame:
        return pl.DataFrame(self._orders, schema=ORDERS) if self._orders else empty(ORDERS)

    def executions(self) -> pl.DataFrame:
        return pl.DataFrame(self._fills, schema=FILLS) if self._fills else empty(FILLS)

    def cancel(self, order_ids: list[int]) -> int:
        n = 0
        for o in self._orders:
            if o["order_id"] in order_ids and o["status"] == "Submitted":
                o["status"], o["remaining"], n = "Cancelled", 0.0, n + 1
        return n

    def cancel_all(self) -> None:
        for o in self._orders:
            if o["status"] == "Submitted":
                o["status"], o["remaining"] = "Cancelled", 0.0

    def close(self) -> None:
        pass

    def fill(self, prices: dict[int, float], fraction: float = 1.0) -> None:
        """Fill open orders at `prices` (con_id -> price); `fraction` < 1 leaves part unfilled."""
        now = datetime.now(UTC)
        for o in self._orders:
            if o["status"] != "Submitted" or o["con_id"] not in prices:
                continue
            q = float(int(o["quantity"] * fraction))
            p = prices[o["con_id"]]
            sign = 1 if o["action"] == "BUY" else -1
            if q:
                self.holdings[o["con_id"]] = self.holdings.get(o["con_id"], 0.0) + sign * q
                comm = q * self.commission_per_share
                self.cash -= sign * q * p + comm
                self._fills.append({"exec_id": f"E{o['order_id']}", "time": now, "account": self.account_no,
                                    "con_id": o["con_id"], "symbol": o["symbol"],
                                    "side": "BOT" if sign > 0 else "SLD", "shares": q, "price": p,
                                    "commission": comm, "order_id": o["order_id"], "perm_id": o["perm_id"],
                                    "order_ref": o["order_ref"]})
            self.marks[o["con_id"]] = p
            o["filled"], o["remaining"] = q, o["quantity"] - q
            o["avg_fill_price"] = p if q else None
            o["status"] = "Filled" if q == o["quantity"] else "Cancelled"
