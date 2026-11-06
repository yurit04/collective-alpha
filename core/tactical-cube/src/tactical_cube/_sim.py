"""One trading day: targets -> orders -> fills -> P&L, on dense security x bar arrays.

Prices within the day are raw. A position is a number of shares (`units`); its value is marked
at each bar's close, at the opening auction and at the official close. A bar's P&L is
units held x the change in mark plus units traded x (mark - fill price), so slippage between
a fill and the bar's close is P&L and costs are charged separately on each fill.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

NY = ZoneInfo("America/New_York")
EPS = 1e-9
FILL_PRICES = ("vwap", "open", "close")
FILLS_SCHEMA = {"security_id": pl.String, "kind": pl.String, "fill_bar": pl.Int32, "fill_time": pl.Datetime("ns", "UTC"),
                "start_minute": pl.Float64, "decision_time": pl.Datetime("ns", "UTC"), "units": pl.Float64,
                "price": pl.Float64, "decision_price": pl.Float64, "wait_bars": pl.Int32,
                "bar_dollar_volume": pl.Float64}
UNFILLED_SCHEMA = {"security_id": pl.String, "decision_time": pl.Datetime("ns", "UTC"), "wanted": pl.Float64,
                   "done": pl.Float64, "reason": pl.String}
CARRY_SCHEMA = {"security_id": pl.String, "units": pl.Float64, "last_close": pl.Float64}
PENDING_SCHEMA = {"security_id": pl.String, "units": pl.Float64, "last_close": pl.Float64,
                  "decision_time": pl.Datetime("ns", "UTC"), "decision_price": pl.Float64}


@dataclass
class DayResult:
    date: date
    row: dict                         # gross, long, short, turnover, exposures, counts (fractions of capital)
    bars: pl.DataFrame                # bar, bar_end, minutes_from_open, pnl, gross_exposure, net_exposure
    fills: pl.DataFrame
    unfilled: pl.DataFrame
    carry: pl.DataFrame               # positions held overnight (shares, today's official close)
    pending: pl.DataFrame             # opening-auction orders for the next session
    by_security: pl.DataFrame         # security_id, pnl (fraction of capital) for the day
    warnings: list[str] = field(default_factory=list)


def _flat_bar(bar_ends: list[datetime], flat_by: str) -> int:
    hh, mm = (int(x) for x in flat_by.split(":"))
    t = time(hh, mm)
    for b, e in enumerate(bar_ends):
        if e.astimezone(NY).time() >= t:
            return b
    return len(bar_ends)


def _events(targets: pl.DataFrame, bar_ends: list[datetime], minutes_to_close: list[int],
            moc_cutoff: int) -> tuple[list[dict], list[dict]]:
    """Group targets into rebalances: kind 'bar' (continuous), 'close' or 'open' (auctions)."""
    pos = {e: b for b, e in enumerate(bar_ends)}
    events, rejected = [], []
    t = targets
    if "auction" not in t.columns:
        t = t.with_columns(auction=pl.lit(None, pl.String))
    bad = set(t["auction"].drop_nulls().unique().to_list()) - {"close", "open"}
    if bad:
        raise ValueError(f"auction must be null, 'close' or 'open', got {sorted(bad)}")
    for (when, auction), g in sorted(t.group_by(["bar_end", "auction"]), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        if when not in pos:
            raise ValueError(f"decision time {when} is not the end of one of the day's bars")
        k = pos[when]
        ev = {"kind": auction or "bar", "k": k, "time": when,
              "book": dict(zip(g["security_id"].to_list(), g["weight"].to_list(), strict=True))}
        if auction == "close" and minutes_to_close[k] < moc_cutoff:
            rejected.append(ev)
        else:
            events.append(ev)
    return events, rejected


def simulate_day(day: date, bars: pl.DataFrame, targets: pl.DataFrame, aux: pl.DataFrame,
                 carry: pl.DataFrame | None = None, pending: pl.DataFrame | None = None, *,
                 capital: float = 1.0, lag: int = 1, fill: str = "vwap", flat_by: str | None = "close",
                 participation: float | None = None, moc_cutoff: int = 10) -> DayResult:
    """Simulate one session.

    bars     nanoprobe grid bars of the day (security_id, bar, bar_end, minutes_from_open,
             minutes_to_close, open, close, vwap, volume, dollar_volume, stale).
    targets  bar_end (decision time: a bar's end), security_id, weight (fraction of capital)
             [, auction: null | 'close' | 'open']. Each (bar_end, auction) group is a rebalance:
             its names get their weight, every other position goes to 0.
             null: fills start `lag` bars later at `fill` prices, capped at `participation` of
             each bar's share volume, working until filled, replaced or the day ends.
             'close': the official closing auction (decided at least `moc_cutoff` minutes before
             the close). 'open': the next session's opening auction.
    aux      security_id, open, close (official, raw), prev_close (today's basis), adv, vol.
    carry    positions from the previous session: security_id, units, last_close.
    pending  opening-auction orders from the previous session (see PENDING_SCHEMA).
    flat_by  'close' (closing auction), 'HH:MM' New York time (orders from that bar, the rest
             in the closing auction), or None (hold overnight).
    """
    if fill not in FILL_PRICES:
        raise ValueError(f"fill must be one of {FILL_PRICES}")
    if lag < 1:
        raise ValueError("lag must be at least 1 bar: a bar's own prices are not tradable at its end")
    warn: list[str] = []
    carry = carry if carry is not None else pl.DataFrame(schema=CARRY_SCHEMA)
    pending = pending if pending is not None else pl.DataFrame(schema=PENDING_SCHEMA)
    clock = (bars.select("bar", "bar_end", "minutes_from_open", "minutes_to_close").unique("bar").sort("bar")
             if bars.height else pl.DataFrame(schema={"bar": pl.Int32, "bar_end": pl.Datetime("ns", "UTC"),
                                                      "minutes_from_open": pl.Int32, "minutes_to_close": pl.Int32}))
    bar_ends = clock["bar_end"].to_list()
    mto = clock["minutes_to_close"].to_list()
    B = len(bar_ends)
    bar_len = np.diff([0, *clock["minutes_from_open"].to_list()]).astype(float)
    start_minute = np.asarray(clock["minutes_from_open"].to_list(), float) - bar_len

    events, rejected = _events(targets, bar_ends, mto, moc_cutoff) if targets.height else ([], [])
    if flat_by is not None and any(e["kind"] == "open" for e in events):
        raise ValueError("opening-auction orders hold positions overnight: use flat_by=None")
    if flat_by is not None and any(e["kind"] == "close" and any(w != 0 for w in e["book"].values()) for e in events):
        raise ValueError(f"flat_by={flat_by!r} holds nothing overnight; closing-auction buys need flat_by=None")

    ids = sorted(set(bars["security_id"].to_list()) | set(carry["security_id"].to_list())
                 | set(pending["security_id"].to_list())
                 | {s for e in events + rejected for s in e["book"]})
    S = len(ids)
    ix = {s: i for i, s in enumerate(ids)}

    def dense(col: str) -> np.ndarray:
        a = np.full((S, B), np.nan)
        if bars.height:
            r = np.fromiter((ix[s] for s in bars["security_id"]), int, bars.height)
            a[r, bars["bar"].to_numpy()] = bars[col].fill_null(np.nan).cast(pl.Float64).to_numpy()
        return a

    close, vol_sh = dense("close"), dense("volume")
    px_all = {"vwap": dense("vwap"), "open": dense("open"), "close": close}
    px = np.where(np.isnan(px_all[fill]), close, px_all[fill])
    dvol = dense("dollar_volume")
    stale = np.ones((S, B), bool)
    if bars.height:
        r = np.fromiter((ix[s] for s in bars["security_id"]), int, bars.height)
        stale[r, bars["bar"].to_numpy()] = bars["stale"].to_numpy()

    def vec(frame: pl.DataFrame, col: str) -> np.ndarray:
        v = np.full(S, np.nan)
        f = frame.filter(pl.col("security_id").is_in(ids))
        if f.height:
            v[[ix[s] for s in f["security_id"]]] = f[col].fill_null(np.nan).cast(pl.Float64).to_numpy()
        return v

    open_auc, close_auc, prev_close = vec(aux, "open"), vec(aux, "close"), vec(aux, "prev_close")

    # ---- overnight: positions and opening-auction orders onto today's share basis
    pos = np.zeros(S)
    basis = np.ones(S)
    if carry.height:
        units, last = vec(carry, "units"), vec(carry, "last_close")
        has = ~np.isnan(units)
        ok = has & (prev_close > 0) & (last > 0)
        basis = np.where(ok, last / np.where(ok, prev_close, 1.0), 1.0)
        pos = np.where(has, np.nan_to_num(units) * basis, 0.0)
        missing = has & np.isnan(prev_close)
        if missing.any():
            warn.append(f"{int(missing.sum())} held position(s) without an official price today, kept at the last close")
    mark = np.where(prev_close > 0, prev_close, vec(carry, "last_close") if carry.height else np.nan)

    fills: list[dict] = []
    unfilled: list[dict] = []
    pnl_bar, long_bar, short_bar = np.zeros(B), np.zeros(B), np.zeros(B)
    gross_bar, net_bar = np.zeros(B), np.zeros(B)
    pnl_open = 0.0
    long_total = short_total = 0.0

    def book_units(book: dict, prices: np.ndarray, base: np.ndarray, when) -> np.ndarray:
        d = np.where(np.abs(base) > EPS, 0.0, np.nan)   # positions not in the book go to 0
        for s, w in book.items():
            i = ix[s]
            p = prices[i]
            if not (p > 0):
                unfilled.append({"security_id": s, "decision_time": when, "wanted": float(w), "done": 0.0,
                                 "reason": "no price at the decision"})
                continue
            d[i] = w * capital / p
        return d

    # opening auction (orders from the previous session)
    o_mark = np.where(open_auc > 0, open_auc, mark)
    hold = np.where(np.isnan(o_mark - mark), 0.0, pos * (o_mark - mark))
    pnl_open += float(np.nansum(hold))
    sec = np.nan_to_num(hold).copy()
    long_total += float(np.nansum(np.where(pos > 0, hold, 0)))
    short_total += float(np.nansum(np.where(pos < 0, hold, 0)))
    mark = o_mark
    if pending.height:
        want = np.where(np.abs(pos) > EPS, 0.0, np.nan)
        p_units = vec(pending, "units")
        listed = ~np.isnan(p_units)
        want[listed] = p_units[listed] * basis[listed]
        dt = pending["decision_time"][0]
        dp = vec(pending, "decision_price")
        for i in np.flatnonzero(~np.isnan(want) & (np.abs(want - pos) > EPS)):
            if not (open_auc[i] > 0):
                unfilled.append({"security_id": ids[i], "decision_time": dt, "wanted": float(want[i]),
                                 "done": float(pos[i]), "reason": "no opening auction"})
                continue
            tr = want[i] - pos[i]
            fills.append({"security_id": ids[i], "kind": "open_auction", "fill_bar": -1, "fill_time": None,
                          "start_minute": 0.0, "decision_time": dt, "units": tr, "price": open_auc[i],
                          "decision_price": dp[i] * basis[i] if listed[i] else np.nan, "wait_bars": None,
                          "bar_dollar_volume": None})
            pos[i] = want[i]

    # continuous orders
    close_ff = close.copy()
    for b in range(B):                       # decision prices: last close so far, else the open mark
        if b:
            close_ff[:, b] = np.where(np.isnan(close_ff[:, b]), close_ff[:, b - 1], close_ff[:, b])
        else:
            close_ff[:, 0] = np.where(np.isnan(close_ff[:, 0]), mark, close_ff[:, 0])
    bar_events = [e for e in events if e["kind"] == "bar"]
    if flat_by not in (None, "close"):
        fb = _flat_bar(bar_ends, flat_by)
        late = [e for e in bar_events if e["k"] >= fb]
        if late:
            warn.append(f"{len(late)} rebalance(s) at or after flat_by {flat_by} ignored")
        bar_events = [e for e in bar_events if e["k"] < fb]
        if fb < B:
            bar_events.append({"kind": "bar", "k": fb, "time": bar_ends[fb], "book": {}, "flat": True})
    activate: dict[int, list[dict]] = {}
    for e in bar_events:
        a = e["k"] + lag
        if a >= B:
            for s, w in e["book"].items():
                unfilled.append({"security_id": s, "decision_time": e["time"], "wanted": float(w), "done": 0.0,
                                 "reason": "after the last bar"})
            continue
        activate.setdefault(a, []).append(e)
    desired = np.full(S, np.nan)
    since = np.full(S, -1)
    dtime = [None] * S
    dprice = np.full(S, np.nan)
    for b in range(B):
        for e in activate.get(b, []):
            # The rebalance replaces the whole book: listed names get their weight, held names
            # not listed go to 0, and orders left from an earlier rebalance are cancelled.
            desired = book_units(e["book"], close_ff[:, e["k"]], pos, e["time"])
            for i in np.flatnonzero(~np.isnan(desired)):
                since[i], dtime[i], dprice[i] = b, e["time"], close_ff[i, e["k"]]
        m = np.where(np.isnan(close[:, b]), mark, close[:, b])
        held = np.where(np.isnan(m - mark), 0.0, pos * (m - mark))
        want = np.where(np.isnan(desired), 0.0, desired - pos)
        can = ~stale[:, b] & (np.abs(want) > EPS) & (px[:, b] > 0)
        if participation is not None:
            cap = participation * np.nan_to_num(vol_sh[:, b])
            tr = np.where(can, np.clip(want, -cap, cap), 0.0)
        else:
            tr = np.where(can, want, 0.0)
        slip = np.where(tr != 0, tr * (m - px[:, b]), 0.0)
        before = pos.copy()
        pos = pos + tr
        pnl = held + slip
        sec += pnl
        sign = np.where(np.abs(pos) > EPS, np.sign(pos), np.sign(before))
        pnl_bar[b] = float(pnl.sum())
        long_bar[b] = float(pnl[sign > 0].sum())
        short_bar[b] = float(pnl[sign < 0].sum())
        for i in np.flatnonzero(tr):
            fills.append({"security_id": ids[i], "kind": "bar", "fill_bar": b, "fill_time": bar_ends[b],
                          "start_minute": float(start_minute[b]), "decision_time": dtime[i], "units": float(tr[i]),
                          "price": float(px[i, b]), "decision_price": float(dprice[i]),
                          "wait_bars": int(b - since[i]), "bar_dollar_volume": float(np.nan_to_num(dvol[i, b]))})
        mark = m
        val = np.where(np.isnan(mark), 0.0, pos * mark)
        gross_bar[b] = float(np.abs(val).sum()) / capital
        net_bar[b] = float(val.sum()) / capital
    # orders still working at the end of continuous trading
    for i in np.flatnonzero(~np.isnan(desired) & (np.abs(desired - pos) > EPS)):
        unfilled.append({"security_id": ids[i], "decision_time": dtime[i], "wanted": float(desired[i]),
                         "done": float(pos[i]), "reason": "not filled by the end of the session"})

    # ---- closing auction
    close_events = [e for e in events if e["kind"] == "close"]
    last_bar_event = max((e["time"] for e in bar_events), default=None)
    close_book = None
    if flat_by is not None:
        close_book = {}
    elif close_events and (last_bar_event is None or close_events[-1]["time"] >= last_bar_event):
        close_book = close_events[-1]["book"]
    for e in rejected:
        for s, w in e["book"].items():
            unfilled.append({"security_id": s, "decision_time": e["time"], "wanted": float(w), "done": 0.0,
                             "reason": f"closing-auction order less than {moc_cutoff} min before the close"})
    c_px = np.where(close_auc > 0, close_auc, mark)
    no_auc = (np.abs(pos) > EPS) & ~(close_auc > 0)
    if no_auc.any():
        warn.append(f"{int(no_auc.sum())} position(s) without an official close, marked at the last bar")
    held = np.where(np.isnan(c_px - mark), 0.0, pos * (c_px - mark))
    pnl_close = float(held.sum())
    sec += held
    long_total += float(held[pos > 0].sum())
    short_total += float(held[pos < 0].sum())
    mark = c_px
    if close_book is not None:
        k = close_events[-1]["k"] if close_events and flat_by is None else B - 1
        when = close_events[-1]["time"] if close_events and flat_by is None else None
        want = book_units(close_book, close_ff[:, k] if B else mark, pos, when)
        for i in np.flatnonzero(~np.isnan(want) & (np.abs(want - pos) > EPS)):
            tr = want[i] - pos[i]
            fills.append({"security_id": ids[i], "kind": "close_auction", "fill_bar": B, "fill_time": None,
                          "start_minute": float(start_minute[-1] if B else 0.0), "decision_time": when,
                          "units": float(tr), "price": float(c_px[i]),
                          "decision_price": float(close_ff[i, k]) if B and when is not None else float(c_px[i]),
                          "wait_bars": None, "bar_dollar_volume": None})
            pos[i] = want[i]

    # ---- opening-auction orders for the next session
    open_events = [e for e in events if e["kind"] == "open"]
    pend = pl.DataFrame(schema=PENDING_SCHEMA)
    if open_events:
        e = open_events[-1]
        ref = close_ff[:, e["k"]]
        want = book_units(e["book"], ref, pos, e["time"])
        rows = [{"security_id": ids[i], "units": float(want[i]), "last_close": float(mark[i]),
                 "decision_time": e["time"], "decision_price": float(ref[i])}
                for i in np.flatnonzero(~np.isnan(want))]
        pend = pl.DataFrame(rows, schema=PENDING_SCHEMA) if rows else pend

    out_carry = pl.DataFrame([{"security_id": ids[i], "units": float(pos[i]), "last_close": float(mark[i])}
                              for i in np.flatnonzero(np.abs(pos) > EPS)], schema=CARRY_SCHEMA)
    fills_df = pl.DataFrame(fills, schema=FILLS_SCHEMA) if fills else pl.DataFrame(schema=FILLS_SCHEMA)
    fills_df = fills_df.with_columns(fill_time=pl.when(pl.col("kind") == "close_auction")
                                     .then(pl.lit(bar_ends[-1] if B else None, pl.Datetime("ns", "UTC")))
                                     .otherwise(pl.col("fill_time")))
    total = pnl_open + pnl_bar.sum() + pnl_close
    overnight = float(np.abs(np.where(np.isnan(mark), 0.0, pos * mark)).sum()) / capital
    turnover = float((fills_df["units"].abs() * fills_df["price"]).sum()) / capital if fills_df.height else 0.0
    row = {"date": day, "gross": total / capital,
           "long": (long_total + long_bar.sum()) / capital, "short": (short_total + short_bar.sum()) / capital,
           "turnover": turnover,
           "gross_exposure": float(gross_bar.mean()) if B else overnight,
           "net_exposure": float(net_bar.mean()) if B else 0.0,
           "max_gross": float(gross_bar.max()) if B else overnight,
           "overnight_gross": overnight, "fills": fills_df.height,
           "rebalances": len(bar_events) + len(close_events) + len(open_events)}
    bar_frame = clock.with_columns(pnl=pl.Series(pnl_bar / capital), gross_exposure=pl.Series(gross_bar),
                                   net_exposure=pl.Series(net_bar)).with_columns(date=pl.lit(day)) if B else \
        pl.DataFrame(schema={"bar": pl.Int32, "bar_end": pl.Datetime("ns", "UTC"), "minutes_from_open": pl.Int32,
                             "minutes_to_close": pl.Int32, "pnl": pl.Float64, "gross_exposure": pl.Float64,
                             "net_exposure": pl.Float64, "date": pl.Date})
    return DayResult(day, row, bar_frame, fills_df.with_columns(date=pl.lit(day)),
                     (pl.DataFrame(unfilled, schema=UNFILLED_SCHEMA) if unfilled else pl.DataFrame(schema=UNFILLED_SCHEMA))
                     .with_columns(date=pl.lit(day)), out_carry, pend,
                     pl.DataFrame({"security_id": ids, "pnl": sec / capital}, schema={"security_id": pl.String, "pnl": pl.Float64})
                     .filter(pl.col("pnl") != 0).with_columns(date=pl.lit(day)), warn)
