"""Aggregate one session of minute bars into one row per ticker."""

from __future__ import annotations

import polars as pl

from collective_alpha.intraday.session import SessionBounds, minute_of_day

# Estimators that need a dense series are suppressed below these bar counts.
MIN_BARS_RV_1M = 120
MIN_BARS_RV_5M = 24
MIN_BARS_SHAPE = 30  # volume-share and leg returns


def _typical_price() -> pl.Expr:
    return (pl.col("high") + pl.col("low") + pl.col("close")) / 3


def _volume_all() -> pl.Expr:
    return pl.col("volume_reg") + pl.col("volume_pre") + pl.col("volume_post") + pl.col("auction_volume")


def aggregate_session(bars: pl.DataFrame, b: SessionBounds) -> pl.DataFrame:
    """bars: minute rows for a single session with ticker, ts_ny, open, high, low, close, volume,
    transactions. Returns one row per ticker that traded in the regular session."""
    df = bars.with_columns(mod=minute_of_day())
    reg = df.filter((pl.col("mod") >= b.open_min) & (pl.col("mod") < b.close_min)).sort(["ticker", "mod"])
    if reg.height == 0:
        return pl.DataFrame()

    in_open = pl.col("mod") < b.open_window_end
    in_close = pl.col("mod") >= b.close_window_start
    in_mid = (pl.col("mod") >= b.open_window_end) & (pl.col("mod") < b.close_window_start)
    logret = pl.col("close").log().diff()

    agg = reg.group_by("ticker").agg(
        n_bars=pl.len(),
        n_zero_vol=(pl.col("volume") <= 0).sum(),
        first_bar_min=pl.col("mod").min(),
        last_bar_min=pl.col("mod").max(),
        open_reg=pl.col("open").first(),
        close_reg=pl.col("close").last(),
        high_reg=pl.col("high").max(),
        low_reg=pl.col("low").min(),
        volume_reg=pl.col("volume").sum(),
        transactions_reg=pl.col("transactions").sum(),
        dollar_vol_reg=(_typical_price() * pl.col("volume")).sum(),
        volume_open30=pl.col("volume").filter(in_open).sum(),
        volume_close30=pl.col("volume").filter(in_close).sum(),
        or_high=pl.col("high").filter(in_open).max(),
        or_low=pl.col("low").filter(in_open).min(),
        ret_open30=(pl.col("close").filter(in_open).last() / pl.col("open").filter(in_open).first() - 1),
        ret_mid=(pl.col("close").filter(in_mid).last() / pl.col("open").filter(in_mid).first() - 1),
        ret_close30=(pl.col("close").filter(in_close).last() / pl.col("open").filter(in_close).first() - 1),
        rv_1m=logret.pow(2).sum().sqrt(),
        max_abs_1m_ret=logret.abs().max(),
        hl_range_mean=(pl.col("high") / pl.col("low")).log().mean(),
        minutes_traded=pl.col("mod").n_unique(),
    )

    # five-minute realised volatility: last close of each five-minute bucket
    buckets = (
        reg.with_columns(bucket=((pl.col("mod") - b.open_min) // 5).cast(pl.Int32))
        .group_by(["ticker", "bucket"])
        .agg(close=pl.col("close").last())
        .sort(["ticker", "bucket"])
    )
    rv5 = buckets.group_by("ticker").agg(
        rv_5m=pl.col("close").log().diff().pow(2).sum().sqrt(),
        n_buckets=pl.len(),
    )
    agg = agg.join(rv5, on="ticker", how="left")

    # The bar stamped exactly at the close carries the closing auction. It is neither continuous
    # trading nor post-market: report it separately, and take the official close from it.
    outside = df.filter((pl.col("mod") < b.open_min) | (pl.col("mod") >= b.close_min))
    if outside.height:
        ext = outside.group_by("ticker").agg(
            volume_pre=pl.col("volume").filter(pl.col("mod") < b.open_min).sum(),
            volume_post=pl.col("volume").filter(pl.col("mod") > b.close_min).sum(),
            auction_volume=pl.col("volume").filter(pl.col("mod") == b.close_min).sum(),
            auction_close=pl.col("close").filter(pl.col("mod") == b.close_min).last(),
        )
        agg = agg.join(ext, on="ticker", how="left")
    else:
        agg = agg.with_columns(
            volume_pre=pl.lit(0.0),
            volume_post=pl.lit(0.0),
            auction_volume=pl.lit(0.0),
            auction_close=pl.lit(None, dtype=pl.Float64),
        )

    dense_1m = pl.col("n_bars") >= MIN_BARS_RV_1M
    dense_5m = pl.col("n_buckets") >= MIN_BARS_RV_5M
    dense_shape = pl.col("n_bars") >= MIN_BARS_SHAPE
    or_range = pl.col("or_high") - pl.col("or_low")

    out = agg.with_columns(
        date=pl.lit(b.date),
        half_day=pl.lit(b.half_day),
        bar_coverage=pl.col("n_bars") / b.n_minutes,
        volume_pre=pl.col("volume_pre").fill_null(0.0),
        volume_post=pl.col("volume_post").fill_null(0.0),
        auction_volume=pl.col("auction_volume").fill_null(0.0),
        close_final=pl.coalesce(pl.col("auction_close"), pl.col("close_reg")),
        vwap=pl.col("dollar_vol_reg") / pl.col("volume_reg"),
        rv_1m=pl.when(dense_1m).then(pl.col("rv_1m")).otherwise(None),
        rv_5m=pl.when(dense_5m).then(pl.col("rv_5m")).otherwise(None),
        hl_range_mean=pl.when(dense_shape).then(pl.col("hl_range_mean")).otherwise(None),
        or_range_pct=pl.when(dense_shape & (or_range > 0)).then(or_range / pl.col("or_low")).otherwise(None),
        close_vs_or=pl.when(dense_shape & (or_range > 0))
        .then((pl.col("close_reg") - pl.col("or_low")) / or_range)
        .otherwise(None),
    ).with_columns(
        close_to_vwap=pl.when(pl.col("vwap") > 0).then(pl.col("close_reg") / pl.col("vwap") - 1).otherwise(None),
        share_open30=pl.when(dense_shape & (pl.col("volume_reg") > 0))
        .then(pl.col("volume_open30") / pl.col("volume_reg"))
        .otherwise(None),
        share_close30=pl.when(dense_shape & (pl.col("volume_reg") > 0))
        .then(pl.col("volume_close30") / pl.col("volume_reg"))
        .otherwise(None),
        share_pre=pl.when(pl.col("volume_reg") > 0).then(pl.col("volume_pre") / _volume_all()).otherwise(None),
        share_post=pl.when(pl.col("volume_reg") > 0).then(pl.col("volume_post") / _volume_all()).otherwise(None),
        share_auction=pl.when(pl.col("volume_reg") > 0)
        .then(pl.col("auction_volume") / (pl.col("volume_reg") + pl.col("auction_volume")))
        .otherwise(None),
    )
    for c in ("ret_open30", "ret_mid", "ret_close30"):
        out = out.with_columns(pl.when(dense_shape).then(pl.col(c)).otherwise(None).alias(c))
    return out.select(
        "ticker",
        "date",
        "half_day",
        "n_bars",
        "bar_coverage",
        "n_zero_vol",
        "first_bar_min",
        "last_bar_min",
        "open_reg",
        "high_reg",
        "low_reg",
        "close_reg",
        "close_final",
        "auction_close",
        "vwap",
        "close_to_vwap",
        "volume_reg",
        "dollar_vol_reg",
        "transactions_reg",
        "volume_pre",
        "volume_post",
        "auction_volume",
        "volume_open30",
        "volume_close30",
        "share_open30",
        "share_close30",
        "share_pre",
        "share_post",
        "share_auction",
        "rv_1m",
        "rv_5m",
        "hl_range_mean",
        "max_abs_1m_ret",
        "or_high",
        "or_low",
        "or_range_pct",
        "close_vs_or",
        "ret_open30",
        "ret_mid",
        "ret_close30",
    ).sort("ticker")
