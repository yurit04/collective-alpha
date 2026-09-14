"""News flow and sentiment per security per session.

An article is attributed to the session on which it is known by the close: published before
16:00 New York on a session -> that session; otherwise the next session.
"""

from __future__ import annotations

import datetime as dt
import json

import polars as pl

from collective_alpha.features.base import FeatureContext
from collective_alpha.panel.corporate_actions import map_events_to_security

SENTIMENT = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}


def explode_news(news: pl.DataFrame, sessions: list[dt.date]) -> pl.DataFrame:
    """One row per (ticker, article) with session date and sentiment (null when no insight)."""
    n = news.select("id", "published_utc", "tickers", "insights").filter(pl.col("tickers").is_not_null())
    n = n.with_columns(
        ts=pl.col("published_utc")
        .str.to_datetime("%Y-%m-%dT%H:%M:%SZ", time_zone="UTC", strict=False)
        .dt.convert_time_zone("America/New_York"),
        tickers_list=pl.col("tickers").str.json_decode(pl.List(pl.Utf8)),
    ).filter(pl.col("ts").is_not_null())
    n = n.with_columns(
        cutoff=(
            pl.col("ts").dt.replace_time_zone(None).dt.date()
            + pl.when(pl.col("ts").dt.hour() >= 16).then(pl.duration(days=1)).otherwise(pl.duration(days=0))
        )
    )
    sess = pl.DataFrame({"date": sessions}).sort("date")
    n = n.sort("cutoff").join_asof(sess, left_on="cutoff", right_on="date", strategy="forward", check_sortedness=False)
    n = n.filter(pl.col("date").is_not_null())

    def parse_insights(s: str | None) -> list[dict] | None:
        if not s:
            return None
        try:
            return [
                {"ticker": i.get("ticker"), "sentiment": SENTIMENT.get(i.get("sentiment"), 0.0)} for i in json.loads(s)
            ]
        except Exception:  # noqa: BLE001
            return None

    ins = (
        n.select("id", "insights")
        .with_columns(
            parsed=pl.col("insights").map_elements(
                parse_insights, return_dtype=pl.List(pl.Struct({"ticker": pl.Utf8, "sentiment": pl.Float64}))
            )
        )
        .explode("parsed")
        .filter(pl.col("parsed").is_not_null())
        .unnest("parsed")
        .select("id", "ticker", "sentiment")
    )
    ex = (
        n.select("id", "date", "tickers_list")
        .explode("tickers_list")
        .rename({"tickers_list": "ticker"})
        .filter(pl.col("ticker").is_not_null())
    )
    return ex.join(ins, on=["id", "ticker"], how="left")


def news_features(
    news: pl.DataFrame, master: pl.DataFrame, keys: pl.DataFrame, sessions: list[dt.date]
) -> pl.DataFrame:
    ex = explode_news(news, sessions)
    ex = map_events_to_security(ex, master)
    daily = ex.group_by(["security_id", "date"]).agg(
        news_count=pl.len(),
        news_sent=pl.col("sentiment").mean(),
        news_sent_n=pl.col("sentiment").count(),
    )
    out = (
        keys.join(daily, on=["security_id", "date"], how="left")
        .sort(["security_id", "date"])
        .with_columns(
            news_count=pl.col("news_count").fill_null(0),
            news_sent_n=pl.col("news_sent_n").fill_null(0),
        )
    )
    sent_sum = (pl.col("news_sent") * pl.col("news_sent_n")).fill_null(0.0)
    out = out.with_columns(
        news_count_5d=pl.col("news_count").rolling_sum(5, min_samples=1).over("security_id"),
        news_count_21d=pl.col("news_count").rolling_sum(21, min_samples=1).over("security_id"),
        __ss5=sent_sum.rolling_sum(5, min_samples=1).over("security_id"),
        __sn5=pl.col("news_sent_n").rolling_sum(5, min_samples=1).over("security_id"),
        __ss21=sent_sum.rolling_sum(21, min_samples=1).over("security_id"),
        __sn21=pl.col("news_sent_n").rolling_sum(21, min_samples=1).over("security_id"),
    )
    out = out.with_columns(
        news_sent_5d=pl.when(pl.col("__sn5") > 0).then(pl.col("__ss5") / pl.col("__sn5")).otherwise(None),
        news_sent_21d=pl.when(pl.col("__sn21") > 0).then(pl.col("__ss21") / pl.col("__sn21")).otherwise(None),
    ).drop(["__ss5", "__sn5", "__ss21", "__sn21"])
    return out


def build(ctx: FeatureContext) -> pl.DataFrame:
    return news_features(ctx.news, ctx.master, ctx.panel.select("security_id", "date"), ctx.sessions)
