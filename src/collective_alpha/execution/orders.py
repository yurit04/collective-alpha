"""Orders and fills, and the translation from target weights to orders."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Order:
    security_id: str
    ticker: str
    qty: int  # signed shares: + buy, - sell
    ref_price: float  # price used to size the order
    date: dt.date  # decision date (executed on the next session)
    note: str = ""

    @property
    def notional(self) -> float:
        return abs(self.qty) * self.ref_price


@dataclass(frozen=True)
class Fill:
    security_id: str
    ticker: str
    qty: int
    price: float
    date: dt.date
    cost: float = 0.0  # commissions + fees in currency


@dataclass
class Position:
    security_id: str
    ticker: str
    qty: int = 0
    avg_price: float = 0.0
    realized_pnl: float = 0.0

    def apply(self, fill: Fill) -> None:
        if self.qty == 0 or (self.qty > 0) == (fill.qty > 0):
            new_qty = self.qty + fill.qty
            if new_qty != 0:
                self.avg_price = (self.avg_price * self.qty + fill.price * fill.qty) / new_qty
            self.qty = new_qty
        else:
            closing = min(abs(self.qty), abs(fill.qty)) * (1 if self.qty > 0 else -1)
            self.realized_pnl += closing * (fill.price - self.avg_price)
            remaining = self.qty + fill.qty
            if remaining == 0:
                self.qty, self.avg_price = 0, 0.0
            elif (remaining > 0) == (self.qty > 0):
                self.qty = remaining
            else:  # flipped through zero
                self.qty, self.avg_price = remaining, fill.price
        self.realized_pnl -= fill.cost
        self.ticker = fill.ticker


@dataclass
class Book:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)

    def nav(self, prices: dict[str, float]) -> float:
        return self.cash + sum(p.qty * prices.get(sid, p.avg_price) for sid, p in self.positions.items() if p.qty)

    def weights(self, prices: dict[str, float]) -> dict[str, float]:
        nav = self.nav(prices)
        return {sid: p.qty * prices.get(sid, p.avg_price) / nav for sid, p in self.positions.items() if p.qty and nav}


def targets_to_orders(
    targets: dict[str, float],
    book: Book,
    prices: dict[str, float],
    tickers: dict[str, str],
    date: dt.date,
    capital: float | None = None,
    min_notional: float = 200.0,
    lot: int = 1,
    max_participation: float | None = None,
    adv: dict[str, float] | None = None,
) -> list[Order]:
    """Orders that move the book to `targets` (weights of NAV, or of `capital` when given).
    Names not in targets are closed. Tiny trades below min_notional are skipped."""
    base = capital if capital is not None else book.nav(prices)
    out: list[Order] = []
    sids = set(targets) | {s for s, p in book.positions.items() if p.qty}
    for sid in sorted(sids):
        px = prices.get(sid)
        if px is None or px <= 0:
            continue
        cur = book.positions.get(sid).qty if sid in book.positions else 0
        raw = targets.get(sid, 0.0) * base / px
        tgt_qty = int(math.trunc(raw / lot) * lot)  # toward zero: never round a tiny target into a share
        if max_participation and adv and adv.get(sid):
            cap_qty = int(max_participation * adv[sid] / px)
            tgt_qty = int(math.copysign(min(abs(tgt_qty), max(cap_qty, 0)), tgt_qty))
        delta = tgt_qty - cur
        if delta == 0 or abs(delta) * px < min_notional:
            continue
        out.append(Order(sid, tickers.get(sid, sid), delta, px, date))
    return out
