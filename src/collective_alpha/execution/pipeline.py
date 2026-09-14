"""Daily trading pipeline (run after the store is updated for the session):

1. fill orders left pending from the previous run at today's open (paper broker)
2. mark the book at today's close
3. on a rebalance day, compute targets from today's signal, translate to orders vs the current
   book and submit them (they execute at the next open)
"""

from __future__ import annotations

import csv
import datetime as dt
import logging
from pathlib import Path

import polars as pl

from collective_alpha.calendar import previous_trading_day, trading_days
from collective_alpha.config import Settings, get_settings
from collective_alpha.execution.broker import PaperBroker
from collective_alpha.execution.orders import Order, targets_to_orders
from collective_alpha.execution.strategy import StrategyConfig, is_rebalance_day, targets_for_date

log = logging.getLogger(__name__)


def paper_path(settings: Settings, strategy: str) -> Path:
    return settings.data_root / "paper" / f"{strategy}.sqlite"


def session_prices(settings: Settings, date: dt.date) -> tuple[dict[str, float], dict[str, float], dict[str, str]]:
    """(open, close, ticker) by security_id for one session."""
    from collective_alpha.panel.build import load_panel

    p = load_panel(settings, date, date, columns=["ticker", "open", "close"])
    opens = dict(zip(p["security_id"].to_list(), p["open"].to_list(), strict=True))
    closes = dict(zip(p["security_id"].to_list(), p["close"].to_list(), strict=True))
    tickers = dict(zip(p["security_id"].to_list(), p["ticker"].to_list(), strict=True))
    return opens, closes, tickers


def run_session(
    cfg: StrategyConfig, date: dt.date | None = None, settings: Settings | None = None, force_rebalance: bool = False
) -> dict:
    s = settings or get_settings()
    date = date or previous_trading_day()
    if cfg.broker != "paper":
        raise NotImplementedError(f"broker {cfg.broker!r} has no adapter yet; use 'paper' or export orders")
    broker = PaperBroker(paper_path(s, cfg.name), initial_cash=cfg.capital)
    opens, closes, tickers = session_prices(s, date)
    out: dict = {"strategy": cfg.name, "date": str(date)}
    fills = broker.fill_pending(date, opens)
    out["fills"] = len(fills)
    out["unfilled"] = len(broker.pending())
    out["mark"] = broker.mark(date, closes)
    sessions = trading_days(date - dt.timedelta(days=45), date)
    if force_rebalance or is_rebalance_day(cfg, sessions, date):
        targets, diag = targets_for_date(cfg, date, s)
        book = broker.book()
        orders = targets_to_orders(targets, book, closes, tickers, date, capital=None, min_notional=cfg.min_notional)
        broker.cancel_pending()
        broker.submit(orders)
        out["rebalance"] = diag
        out["orders"] = len(orders)
        out["order_notional"] = round(sum(o.notional for o in orders), 2)
    else:
        out["rebalance"] = None
    broker.close()
    return out


def replay(cfg: StrategyConfig, start: dt.date, end: dt.date, settings: Settings | None = None) -> list[dict]:
    """Run the pipeline session by session over a past range (paper broker), e.g. to seed a book."""
    out = []
    for d in trading_days(start, end):
        try:
            out.append(run_session(cfg, d, settings))
        except Exception as e:  # noqa: BLE001
            log.error("%s: %s", d, e)
            out.append({"date": str(d), "error": str(e)})
    return out


def status(cfg: StrategyConfig, settings: Settings | None = None) -> dict:
    s = settings or get_settings()
    p = paper_path(s, cfg.name)
    if not p.exists():
        return {"strategy": cfg.name, "state": "no paper book yet"}
    b = PaperBroker(p, initial_cash=cfg.capital)
    book = b.book()
    hist = b.nav_history()
    pend = b.pending()
    out = {
        "strategy": cfg.name,
        "cash": round(book.cash, 2),
        "positions": sum(1 for x in book.positions.values() if x.qty),
        "pending_orders": len(pend),
        "nav_last": hist[-1][:2] if hist else None,
        "nav_first": hist[0][:2] if hist else None,
        "realized_pnl": round(sum(x.realized_pnl for x in book.positions.values()), 2),
    }
    b.close()
    return out


def export_orders(cfg: StrategyConfig, path: Path, settings: Settings | None = None) -> int:
    """Write pending orders to CSV (ticker, side, qty, ref_price) for manual execution."""
    s = settings or get_settings()
    b = PaperBroker(paper_path(s, cfg.name), initial_cash=cfg.capital)
    pend = b.pending()
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ticker", "side", "qty", "ref_price", "notional", "security_id", "decision_date"])
        for _, o in pend:
            w.writerow(
                [
                    o.ticker,
                    "BUY" if o.qty > 0 else "SELL",
                    abs(o.qty),
                    round(o.ref_price, 4),
                    round(o.notional, 2),
                    o.security_id,
                    o.date,
                ]
            )
    b.close()
    return len(pend)


def nav_frame(cfg: StrategyConfig, settings: Settings | None = None) -> pl.DataFrame:
    s = settings or get_settings()
    b = PaperBroker(paper_path(s, cfg.name), initial_cash=cfg.capital)
    rows = b.nav_history()
    b.close()
    return pl.DataFrame(rows, schema=["date", "nav", "cash", "gross", "net", "n_positions"], orient="row").with_columns(
        pl.col("date").str.to_date()
    )


__all__ = ["run_session", "replay", "status", "export_orders", "nav_frame", "Order"]
