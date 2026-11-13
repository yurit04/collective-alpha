"""Cost measures for one day of one block of tickers: trades matched to the NBBO in force.

Definitions (all fractions of the prevailing midpoint m, reported in basis points; half-spreads,
so a round trip costs twice the number):
- quoted half-spread (ask - bid) / 2m, weighted by the time each usable quote was in force
  during the regular session;
- effective half-spread |p - m| / m per trade, weighted by dollar volume;
- realized half-spread d (p - m_h) / m and price impact d (m_h - m) / m, with d the trade sign
  and m_h the midpoint h later (1 and 5 minutes, at most the close); effective = realized +
  impact;
- trade sign d: Lee and Ready, +1 above the midpoint, -1 below, the tick test at the midpoint.

Minute rows carry their weights (`q_ns`, `mdv`), so 30-minute buckets and days are weighted means
of minutes; `daily()` here adds what minutes cannot (venue splits, auctions, quotes at fixed
times).
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from ._clean import (
    ISO,
    ODD_LOT,
    TRF_EXCHANGE,
    WHOLE_SECOND_ERA,
    classify_trades,
    has_any,
    interpolate_seconds,
    match_time,
    usable_quote,
)

MINUTE = 60_000_000_000
HORIZONS = {"1m": 60_000_000_000, "5m": 300_000_000_000}
MAX_EFFECTIVE = 0.25   # |p - m| / m above this is a bad print or a bad quote: left out of measures
BPS = 1e4

# Fixed times for quote snapshots, as offsets from the open (+) or the close (-), in minutes.
SNAPSHOTS = {"open1": ("open", 1), "close10": ("close", -10), "preclose": ("close", 0)}


@dataclass
class DayBlock:
    minute: pl.DataFrame
    daily: pl.DataFrame
    trades: pl.DataFrame


def second_stamped(quotes: pl.LazyFrame, sample: int = 1_000_000) -> bool:
    """True when the day's quote timestamps are whole seconds (Holden-Jacobsen interpolation)."""
    share = (quotes.select("sip_timestamp").head(sample)
             .select(((pl.col("sip_timestamp").dt.epoch("ns") % 1_000_000_000) == 0).mean()).collect().item())
    return bool(share is not None and share >= WHOLE_SECOND_ERA)


def prepare_quotes(quotes: pl.LazyFrame, interp: bool) -> pl.LazyFrame:
    q = quotes.select("ticker", pl.col("sip_timestamp").dt.epoch("ns").alias("ts"),
                      pl.col("bid_price").alias("bid"), pl.col("ask_price").alias("ask"),
                      pl.col("bid_size").alias("bsz"), pl.col("ask_size").alias("asz"))
    if interp:
        q = interpolate_seconds(q, "ts")
    return q.filter(usable_quote())


def prepare_trades(trades: pl.LazyFrame, interp: bool) -> pl.LazyFrame:
    t = classify_trades(trades).select(
        "ticker", match_time(), "price", "size", "exchange", "conditions", "kind")
    if interp:
        t = interpolate_seconds(t.rename({"t": "ts"}), "ts", only_whole=True).rename({"ts": "t"})
    return t


def quote_minutes(q: pl.DataFrame, open_ns: int, close_ns: int) -> pl.DataFrame:
    """Time-weighted quote measures per ticker and session minute (exact: a quote in force across
    several minutes counts in each), plus the quote in force at each minute's end."""
    q = q.sort("ticker", "ts")
    seg = (q.with_columns(pl.col("ts").shift(-1).over("ticker").fill_null(close_ns).alias("end"))
           .with_columns(pl.col("ts").clip(open_ns, close_ns).alias("start"), pl.col("end").clip(open_ns, close_ns))
           .filter(pl.col("end") > pl.col("start"))
           .with_columns(pl.int_ranges((pl.col("start") - open_ns) // MINUTE,
                                       (pl.col("end") - 1 - open_ns) // MINUTE + 1).alias("minute"))
           .explode("minute")
           .with_columns(pl.col("minute").cast(pl.Int16))
           .with_columns((pl.min_horizontal(pl.col("end"), open_ns + (pl.col("minute").cast(pl.Int64) + 1) * MINUTE)
                          - pl.max_horizontal(pl.col("start"), open_ns + pl.col("minute").cast(pl.Int64) * MINUTE))
                         .alias("dur")))
    mid = (pl.col("bid") + pl.col("ask")) / 2
    hs = (pl.col("ask") - pl.col("bid")) / (2 * mid) * BPS
    w = pl.col("dur").cast(pl.Float64)
    return (seg.group_by("ticker", "minute").agg(
        pl.col("dur").sum().alias("q_ns"),
        ((hs * w).sum() / w.sum()).alias("qhs_bps"),
        (((pl.col("ask") - pl.col("bid")) * w).sum() / w.sum()).alias("qspread"),
        (((pl.col("bid") * pl.col("bsz") + pl.col("ask") * pl.col("asz")) / 2 * w).sum() / w.sum()).alias("depth_usd"),
        pl.col("bid").sort_by("start").last().alias("bid_end"),
        pl.col("ask").sort_by("start").last().alias("ask_end"),
        pl.len().alias("q_segments"),
    ))


def match(t: pl.DataFrame, q: pl.DataFrame, close_ns: int) -> pl.DataFrame:
    """Trades with the usable NBBO in force just before them and the midpoints 1 and 5 minutes on."""
    qq = q.select("ticker", "ts", "bid", "ask").sort("ts")
    t = t.sort("t")
    m = t.join_asof(qq, left_on="t", right_on="ts", by="ticker", strategy="backward",
                    allow_exact_matches=False, check_sortedness=False).rename({"ts": "q_ts"})
    mids = qq.select("ticker", "ts", ((pl.col("bid") + pl.col("ask")) / 2).alias("m"))
    for name, h in HORIZONS.items():
        m = (m.with_columns(pl.min_horizontal(pl.col("t") + h, pl.lit(close_ns)).alias("__th"))
             .join_asof(mids.rename({"m": f"mid_{name}"}), left_on="__th", right_on="ts", by="ticker",
                        strategy="backward", check_sortedness=False)
             .drop("__th", "ts"))
    return m


def sign_trades(m: pl.DataFrame) -> pl.DataFrame:
    """Lee-Ready sign; ties at the midpoint by the tick test over the ticker's continuous trades."""
    m = m.sort("ticker", "t").with_columns(
        pl.col("price").diff().over("ticker").sign().replace(0, None).alias("__tick"))
    m = m.with_columns(pl.col("__tick").forward_fill().over("ticker"))
    mid = (pl.col("bid") + pl.col("ask")) / 2
    d = (pl.when(pl.col("price") > mid).then(1).when(pl.col("price") < mid).then(-1)
         .otherwise(pl.col("__tick")).fill_null(0).cast(pl.Int8))
    return m.with_columns(d.alias("sign")).drop("__tick")


def trade_measures(m: pl.DataFrame) -> pl.DataFrame:
    mid = (pl.col("bid") + pl.col("ask")) / 2
    out = m.with_columns(
        (pl.col("price") * pl.col("size")).alias("dv"),
        ((pl.col("price") - mid).abs() / mid).alias("eff"),
    ).with_columns(
        (pl.col("eff").is_not_null() & (pl.col("eff") <= MAX_EFFECTIVE)).alias("ok"),
    )
    cols = []
    for name in HORIZONS:
        cols += [(pl.col("sign") * (pl.col("price") - pl.col(f"mid_{name}")) / mid).alias(f"rs_{name}"),
                 (pl.col("sign") * (pl.col(f"mid_{name}") - mid) / mid).alias(f"pi_{name}")]
    return out.with_columns(cols)


def _dw(col: str) -> pl.Expr:
    """Dollar-weighted mean of `col` over usable matched trades, in bps."""
    w = pl.when(pl.col("ok")).then(pl.col("dv")).otherwise(0.0)
    return pl.when(w.sum() > 0).then((pl.col(col) * w).sum() / w.sum() * BPS).alias(f"{col}_bps")


def trade_minutes(tm: pl.DataFrame, open_ns: int) -> pl.DataFrame:
    tm = tm.with_columns(((pl.col("t") - open_ns) // MINUTE).cast(pl.Int16).alias("minute"))
    return tm.group_by("ticker", "minute").agg(
        pl.len().alias("n_trades"),
        pl.col("size").sum().alias("volume"),
        pl.col("dv").sum().alias("dollar_volume"),
        pl.col("dv").filter(pl.col("sign") > 0).sum().alias("buy_dv"),
        pl.col("dv").filter(pl.col("sign") < 0).sum().alias("sell_dv"),
        pl.col("dv").filter(pl.col("exchange") == TRF_EXCHANGE).sum().alias("trf_dv"),
        pl.col("dv").filter(pl.col("ok")).sum().alias("mdv"),
        _dw("eff"), *[_dw(f"{k}_{h}") for h in HORIZONS for k in ("rs", "pi")],
    )


def trade_daily(tm: pl.DataFrame) -> pl.DataFrame:
    lit = pl.col("exchange") != TRF_EXCHANGE
    w = pl.when(pl.col("ok")).then(pl.col("dv")).otherwise(0.0)
    venue = []
    for name, cond in (("lit", lit), ("trf", ~lit)):
        wv = pl.when(cond).then(w).otherwise(0.0)
        venue.append(pl.when(wv.sum() > 0).then((pl.col("eff") * wv).sum() / wv.sum() * BPS).alias(f"eff_{name}_bps"))
    okc = pl.col("ok")
    return tm.group_by("ticker").agg(
        pl.len().alias("n_trades"),
        okc.sum().alias("n_matched"),
        (pl.col("eff").filter(okc).mean() * BPS).alias("eff_ew_bps"),
        *venue,
        ((pl.col("price") < pl.col("bid")) | (pl.col("price") > pl.col("ask"))).filter(okc).mean().alias("outside_share"),
        (pl.col("dv").filter(has_any("conditions", (ODD_LOT,))).sum() / pl.col("dv").sum()).alias("odd_lot_share"),
        (pl.col("dv").filter(has_any("conditions", (ISO,))).sum() / pl.col("dv").sum()).alias("iso_share"),
        (pl.col("dv").filter(pl.col("sign") == 0).sum() / pl.col("dv").sum()).alias("unsigned_share"),
    )


def auctions(t: pl.DataFrame) -> pl.DataFrame:
    """Opening and closing auction print (the largest one) and the volume of all such prints."""
    out = []
    for kind, name in ((1, "open"), (2, "close")):
        a = (t.filter(pl.col("kind") == kind).group_by("ticker").agg(
            pl.col("price").sort_by("size").last().alias(f"{name}_auction_price"),
            pl.col("size").sum().alias(f"{name}_auction_volume")))
        out.append(a)
    return out[0].join(out[1], on="ticker", how="full", coalesce=True)


def snapshots(q: pl.DataFrame, tickers: pl.Series, open_ns: int, close_ns: int) -> pl.DataFrame:
    """Usable NBBO in force at fixed times (just before them): mid and half-spread."""
    rows = []
    for name, (anchor, minutes) in SNAPSHOTS.items():
        at = (open_ns if anchor == "open" else close_ns) + minutes * MINUTE
        rows.append(pl.DataFrame({"ticker": tickers, "__at": at, "__name": name}))
    grid = pl.concat(rows).sort("__at")
    qq = q.select("ticker", "ts", "bid", "ask").sort("ts")
    s = grid.join_asof(qq, left_on="__at", right_on="ts", by="ticker", strategy="backward",
                       allow_exact_matches=False, check_sortedness=False)
    mid = (pl.col("bid") + pl.col("ask")) / 2
    s = s.with_columns(mid.alias("mid"), ((pl.col("ask") - pl.col("bid")) / (2 * mid) * BPS).alias("hs"))
    wide = s.pivot(on="__name", index="ticker", values=["mid", "hs"])
    return wide.rename({c: f"q{c}_bps" for c in wide.columns if c.startswith("hs_")})


def measure_block(trades: pl.LazyFrame, quotes: pl.LazyFrame, open_ns: int, close_ns: int, interp: bool,
                  keep: set[str] | None = None, official: pl.DataFrame | None = None) -> DayBlock:
    """All measures for the tickers in `trades`/`quotes` (one day). `keep`: tickers whose matched
    trades are returned (None: none). `official`: ticker, official_open, official_close (the day
    bar's open and close); auction comparisons use them, else the largest auction print (none
    is flagged in the 2000s)."""
    q = prepare_quotes(quotes, interp).collect()
    t = prepare_trades(trades, interp).collect()

    qmin = quote_minutes(q, open_ns, close_ns)
    cont = t.filter((pl.col("kind") == 0) & (pl.col("t") >= open_ns) & (pl.col("t") < close_ns))
    tm = trade_measures(sign_trades(match(cont, q, close_ns)))
    tmin = trade_minutes(tm, open_ns)
    minute = (qmin.join(tmin, on=["ticker", "minute"], how="full", coalesce=True)
              .with_columns(pl.col("minute").cast(pl.Int16)).sort("ticker", "minute"))

    tickers = pl.concat([q.select("ticker").unique(), t.select("ticker").unique()]).unique().sort("ticker")["ticker"]
    qd = _daily_from_minutes(minute)
    daily = (pl.DataFrame({"ticker": tickers})
             .join(qd, on="ticker", how="left")
             .join(trade_daily(tm), on="ticker", how="left", suffix="_all")
             .join(auctions(t), on="ticker", how="left")
             .join(snapshots(q, tickers, open_ns, close_ns), on="ticker", how="left")
             .join(q.group_by("ticker").agg(pl.len().alias("n_quotes")), on="ticker", how="left"))
    if official is None:
        official = pl.DataFrame(schema={"ticker": pl.String, "official_open": pl.Float64, "official_close": pl.Float64})
    daily = daily.join(official.select("ticker", "official_open", "official_close"), on="ticker", how="left")
    close = pl.coalesce("official_close", "close_auction_price")
    opn = pl.coalesce("official_open", "open_auction_price")
    daily = daily.with_columns(
        ((close / pl.col("mid_preclose") - 1) * BPS).alias("close_vs_mid_bps"),
        ((close / pl.col("mid_close10") - 1) * BPS).alias("close_vs_mid10_bps"),
        ((opn / pl.col("mid_open1") - 1) * BPS).alias("open_vs_mid1_bps"),
    )

    kept = pl.DataFrame()
    if keep:
        aux = t.filter(pl.col("kind").is_in([1, 2, 3]) & pl.col("ticker").is_in(list(keep)))
        kept = pl.concat([
            tm.filter(pl.col("ticker").is_in(list(keep))).select(
                "ticker", "t", "price", "size", "exchange", "conditions", "kind", "bid", "ask", "sign",
                "mid_1m", "mid_5m", ((pl.col("t") - pl.col("q_ts")) // 1000).alias("quote_age_us")),
            aux.select("ticker", "t", "price", "size", "exchange", "conditions", "kind"),
        ], how="diagonal_relaxed").sort("ticker", "t").with_columns(
            pl.from_epoch("t", time_unit="ns").dt.replace_time_zone("UTC"))
    return DayBlock(minute=minute, daily=daily, trades=kept)


def _daily_from_minutes(minute: pl.DataFrame) -> pl.DataFrame:
    qw = pl.col("q_ns").cast(pl.Float64).fill_null(0.0)
    tw = pl.col("mdv").fill_null(0.0)

    def wmean(col: str, w: pl.Expr) -> pl.Expr:
        wn = w.filter(pl.col(col).is_not_null()).sum()
        return pl.when(wn > 0).then((pl.col(col) * w).sum() / wn).alias(col)

    return minute.group_by("ticker").agg(
        pl.col("q_ns").sum().alias("q_ns"),
        wmean("qhs_bps", qw), wmean("qspread", qw), wmean("depth_usd", qw),
        pl.col("volume").sum(), pl.col("dollar_volume").sum(), pl.col("buy_dv").sum(), pl.col("sell_dv").sum(),
        pl.col("trf_dv").sum(), pl.col("mdv").sum(),
        wmean("eff_bps", tw), *[wmean(f"{k}_{h}_bps", tw) for h in HORIZONS for k in ("rs", "pi")],
    )
