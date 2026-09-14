"""Broker interface and a paper broker with a persistent SQLite ledger.

The paper broker fills orders at a reference price (the next session's open by default) with a
slippage/commission model, marks positions at the close, and records NAV daily. Real broker
adapters implement the same interface.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path

from collective_alpha.execution.orders import Book, Fill, Order, Position

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (security_id TEXT PRIMARY KEY, ticker TEXT, qty INTEGER, avg_price REAL, realized_pnl REAL);
CREATE TABLE IF NOT EXISTS cash (id INTEGER PRIMARY KEY CHECK (id = 1), cash REAL);
CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, security_id TEXT, ticker TEXT, qty INTEGER,
    ref_price REAL, status TEXT, note TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS fills (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, date TEXT, security_id TEXT, ticker TEXT,
    qty INTEGER, price REAL, cost REAL);
CREATE TABLE IF NOT EXISTS nav (date TEXT PRIMARY KEY, nav REAL, cash REAL, gross REAL, net REAL, n_positions INTEGER, detail TEXT);
"""


class Broker(ABC):
    @abstractmethod
    def book(self) -> Book: ...

    @abstractmethod
    def submit(self, orders: list[Order]) -> list[int]: ...

    @abstractmethod
    def pending(self) -> list[tuple[int, Order]]: ...


class PaperBroker(Broker):
    def __init__(
        self, path: Path, initial_cash: float = 1_000_000.0, commission_bps: float = 0.5, slippage_bps: float = 3.0
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.commission_bps = commission_bps
        self.slippage_bps = slippage_bps
        self._c = sqlite3.connect(path)
        self._c.executescript(SCHEMA)
        if self._c.execute("SELECT cash FROM cash WHERE id=1").fetchone() is None:
            self._c.execute("INSERT INTO cash(id, cash) VALUES (1, ?)", (initial_cash,))
            self._c.commit()

    # ----------------------------------------------------------------- state
    def book(self) -> Book:
        cash = self._c.execute("SELECT cash FROM cash WHERE id=1").fetchone()[0]
        b = Book(cash=cash)
        for sid, tk, qty, ap, rp in self._c.execute(
            "SELECT security_id, ticker, qty, avg_price, realized_pnl FROM positions"
        ):
            b.positions[sid] = Position(sid, tk, qty, ap, rp)
        return b

    def _save(self, book: Book) -> None:
        self._c.execute("UPDATE cash SET cash=? WHERE id=1", (book.cash,))
        for sid, p in book.positions.items():
            self._c.execute(
                "INSERT INTO positions VALUES (?,?,?,?,?) ON CONFLICT(security_id) DO UPDATE SET ticker=excluded.ticker, qty=excluded.qty, avg_price=excluded.avg_price, realized_pnl=excluded.realized_pnl",
                (sid, p.ticker, p.qty, p.avg_price, p.realized_pnl),
            )
        self._c.commit()

    # ----------------------------------------------------------------- orders
    def submit(self, orders: list[Order]) -> list[int]:
        ids = []
        now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        for o in orders:
            cur = self._c.execute(
                "INSERT INTO orders(date, security_id, ticker, qty, ref_price, status, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (o.date.isoformat(), o.security_id, o.ticker, o.qty, o.ref_price, "pending", o.note, now),
            )
            ids.append(int(cur.lastrowid))
        self._c.commit()
        return ids

    def pending(self) -> list[tuple[int, Order]]:
        rows = self._c.execute(
            "SELECT id, date, security_id, ticker, qty, ref_price, note FROM orders WHERE status='pending' ORDER BY id"
        ).fetchall()
        return [(i, Order(sid, tk, qty, rp, dt.date.fromisoformat(d), note)) for i, d, sid, tk, qty, rp, note in rows]

    def cancel_pending(self) -> int:
        n = self._c.execute("UPDATE orders SET status='cancelled' WHERE status='pending'").rowcount
        self._c.commit()
        return n

    # ----------------------------------------------------------------- simulation
    def fill_pending(self, date: dt.date, prices: dict[str, float]) -> list[Fill]:
        """Fill every pending order at `prices` (e.g. the session's open) with slippage against the
        trade direction and commission. Orders whose security has no price stay pending."""
        book = self.book()
        fills = []
        for oid, o in self.pending():
            px = prices.get(o.security_id)
            if px is None or px <= 0:
                continue
            side = 1 if o.qty > 0 else -1
            fill_px = px * (1 + side * self.slippage_bps / 1e4)
            cost = abs(o.qty) * fill_px * self.commission_bps / 1e4
            f = Fill(o.security_id, o.ticker, o.qty, fill_px, date, cost)
            pos = book.positions.setdefault(o.security_id, Position(o.security_id, o.ticker))
            pos.apply(f)
            book.cash -= o.qty * fill_px + cost
            self._c.execute(
                "INSERT INTO fills(order_id, date, security_id, ticker, qty, price, cost) VALUES (?,?,?,?,?,?,?)",
                (oid, date.isoformat(), f.security_id, f.ticker, f.qty, f.price, f.cost),
            )
            self._c.execute("UPDATE orders SET status='filled' WHERE id=?", (oid,))
            fills.append(f)
        self._save(book)
        return fills

    def mark(self, date: dt.date, prices: dict[str, float]) -> dict:
        book = self.book()
        nav = book.nav(prices)
        w = book.weights(prices)
        gross = sum(abs(x) for x in w.values())
        net = sum(w.values())
        n = sum(1 for p in book.positions.values() if p.qty)
        self._c.execute(
            "INSERT INTO nav VALUES (?,?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET nav=excluded.nav, cash=excluded.cash, gross=excluded.gross, net=excluded.net, n_positions=excluded.n_positions, detail=excluded.detail",
            (date.isoformat(), nav, book.cash, gross, net, n, json.dumps({k: round(v, 5) for k, v in w.items()})),
        )
        self._c.commit()
        return {
            "date": date.isoformat(),
            "nav": round(nav, 2),
            "cash": round(book.cash, 2),
            "gross": round(gross, 3),
            "net": round(net, 3),
            "positions": n,
        }

    def nav_history(self) -> list[tuple]:
        return self._c.execute("SELECT date, nav, cash, gross, net, n_positions FROM nav ORDER BY date").fetchall()

    def close(self) -> None:
        self._c.close()
