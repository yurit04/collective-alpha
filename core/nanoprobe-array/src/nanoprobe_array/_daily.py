"""Official daily prices (auctions) and one summary row per security and day."""

from __future__ import annotations

import polars as pl

from ._bars import resample

DAILY_COLUMNS = ["date", "security_id", "ticker", "open", "high", "low", "close", "volume",
                 "prev_close", "prev_date"]
SUMMARY_COLUMNS = ["date", "security_id", "ticker", "open", "close", "prev_close", "volume",
                   "minute_volume", "auction_share", "minutes_traded", "vwap", "high", "low",
                   "ret_overnight", "ret_intraday", "ret_first30", "ret_last30", "ret_close_auction",
                   "rv_5min", "bars_5min"]


def daily_prices(adjusted: pl.DataFrame | pl.LazyFrame) -> pl.DataFrame:
    """Daily bars from `Store.bars(..., adjust="all")` -> raw official prices of each day and the
    previous trading day's close on that day's share basis (splits and dividends between the two
    days taken out), so `open / prev_close - 1` is the overnight total return.

    The factors only use actions effective by the later day, so prev_close is known at its open.
    A security's first row (or the first after a new security_id) has no prev_close.
    """
    f = pl.col("split_factor") * pl.col("div_factor")
    return (
        adjusted.lazy()
        .unique(["security_id", "date"], keep="last")
        .sort("security_id", "date")
        .with_columns(_f=f)
        .with_columns(prev_close=(pl.col("close").shift(1) / pl.col("_f")).over("security_id"),
                      prev_date=pl.col("date").shift(1).over("security_id"),
                      **{c: pl.col(c) / pl.col("_f") for c in ("open", "high", "low", "close")})
        .select(DAILY_COLUMNS)
        .collect()
    )


def summarize(minutes: pl.DataFrame | pl.LazyFrame, sessions: pl.DataFrame, daily: pl.DataFrame) -> pl.DataFrame:
    """One row per security and day from that day's minute bars and `daily_prices`.

    open/close/volume are official (auctions included); minute_volume, vwap, high, low and
    minutes_traded come from regular-session minutes. ret_first30 runs from the open to the last
    print before open + 30 min; ret_last30 from the last print before close - 30 min to the
    official close; ret_close_auction from the last regular minute's close to the official close.
    auction_share = 1 - minute_volume / volume (the closing auction and other prints outside
    the minute bars). rv_5min: sqrt of summed squared log returns between consecutive 5-minute
    closes that day (bars_5min of them traded). Securities without regular minutes or without a
    daily bar are left out.
    """
    lf = minutes.lazy()
    ses = sessions.lazy().select("date", "market_open", "market_close")
    ts = pl.col("timestamp")
    reg = (lf.join(ses, on="date")
           .filter((ts >= pl.col("market_open")) & (ts < pl.col("market_close")))
           .sort("security_id", "timestamp"))
    tp_v = (pl.col("high") + pl.col("low") + pl.col("close")) / 3 * pl.col("volume")
    agg = reg.group_by("date", "security_id").agg(
        pl.col("ticker").last().alias("_ticker"),
        pl.len().cast(pl.Int32).alias("minutes_traded"),
        pl.col("volume").sum().alias("minute_volume"),
        (tp_v.sum() / pl.col("volume").sum()).alias("vwap"),
        pl.col("high").max().alias("high"), pl.col("low").min().alias("low"),
        pl.col("close").filter(ts < pl.col("market_open") + pl.duration(minutes=30)).last().alias("_p30"),
        pl.col("close").filter(ts < pl.col("market_close") - pl.duration(minutes=30)).last().alias("_plast30"),
        pl.col("close").last().alias("_last_minute"),
    )
    five = resample(lf, sessions, 5).lazy()
    rv = (five.sort("security_id", "date", "bar")
          .with_columns(r=(pl.col("close") / pl.col("close").shift(1)).log().over("security_id", "date"))
          .group_by("date", "security_id")
          .agg(rv_5min=pl.col("r").pow(2).sum().sqrt(), bars_5min=pl.len().cast(pl.Int32)))
    d = daily.lazy().select("date", "security_id", "ticker", "open", "close", "volume", "prev_close")
    return (
        agg.join(d, on=["date", "security_id"], how="inner")
        .join(rv, on=["date", "security_id"], how="left")
        .with_columns(ticker=pl.coalesce("ticker", "_ticker"),
                      vwap=pl.when(pl.col("minute_volume") > 0).then(pl.col("vwap")),
                      auction_share=pl.when(pl.col("volume") > 0).then(1 - pl.col("minute_volume") / pl.col("volume")),
                      ret_overnight=pl.col("open") / pl.col("prev_close") - 1,
                      ret_intraday=pl.col("close") / pl.col("open") - 1,
                      ret_first30=pl.col("_p30") / pl.col("open") - 1,
                      ret_last30=pl.col("close") / pl.col("_plast30") - 1,
                      ret_close_auction=pl.col("close") / pl.col("_last_minute") - 1)
        .select(SUMMARY_COLUMNS)
        .sort("date", "security_id")
        .collect()
    )
