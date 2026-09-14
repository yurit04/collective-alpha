"""Signal diagnostics on a long frame (security_id, date, signal, fwd_ret...)."""

from __future__ import annotations

import math

import polars as pl


def rank_ic(df: pl.DataFrame, signal: str = "signal", fwd: str = "fwd_ret_1d", min_names: int = 20) -> pl.DataFrame:
    """Per-date Spearman correlation between signal and forward return."""
    d = df.filter(pl.col(signal).is_not_null() & pl.col(fwd).is_not_null())
    return (
        d.group_by("date")
        .agg(
            ic=pl.corr(pl.col(signal).rank(), pl.col(fwd).rank()),
            n=pl.len(),
        )
        .filter(pl.col("n") >= min_names)
        .sort("date")
    )


def ic_summary(ic: pl.DataFrame, horizon: int = 1) -> dict:
    """Mean IC, its t-stat (Newey-West-free; overlapping horizons handled by scaling), IR, hit rate."""
    s = ic["ic"].drop_nulls()
    n = s.len()
    if n < 2:
        return {"n_dates": n}
    mean, std = float(s.mean()), float(s.std())
    # for overlapping h-day forward returns, effective sample ~ n / h
    n_eff = max(n / horizon, 2)
    t = mean / (std / math.sqrt(n_eff)) if std > 0 else float("nan")
    return {
        "n_dates": n,
        "ic_mean": round(mean, 4),
        "ic_std": round(std, 4),
        "ic_ir": round(mean / std, 3) if std > 0 else float("nan"),
        "ic_tstat": round(t, 2),
        "hit_rate": round(float((s > 0).mean()), 3),
        "names_per_date": int(ic["n"].median()),
    }


def ic_decay(
    df: pl.DataFrame, signal: str = "signal", horizons: tuple[int, ...] = (1, 2, 3, 5, 10, 21, 42, 63)
) -> pl.DataFrame:
    rows = []
    for h in horizons:
        col = f"fwd_ret_{h}d"
        if col not in df.columns:
            continue
        ic = rank_ic(df, signal, col)
        s = ic_summary(ic, h)
        rows.append(
            {
                "horizon": h,
                "ic_mean": s.get("ic_mean"),
                "ic_ir": s.get("ic_ir"),
                "ic_tstat": s.get("ic_tstat"),
                "hit_rate": s.get("hit_rate"),
            }
        )
    return pl.DataFrame(rows)


def quantile_returns(
    df: pl.DataFrame, signal: str = "signal", fwd: str = "fwd_ret_1d", n_quantiles: int = 10, min_names: int = 20
) -> pl.DataFrame:
    """Per-date equal-weight mean forward return by signal quantile (1 = lowest signal)."""
    d = df.filter(pl.col(signal).is_not_null() & pl.col(fwd).is_not_null())
    d = d.with_columns(
        q=(
            (pl.col(signal).rank(method="ordinal").over("date") - 1)
            * n_quantiles
            // pl.col(signal).count().over("date")
            + 1
        ).cast(pl.Int32),
        n=pl.len().over("date"),
    ).filter(pl.col("n") >= min_names)
    return d.group_by(["date", "q"]).agg(ret=pl.col(fwd).mean(), names=pl.len()).sort(["date", "q"])


def long_short(qret: pl.DataFrame, n_quantiles: int = 10) -> pl.DataFrame:
    """Top-minus-bottom quantile spread per date."""
    top = qret.filter(pl.col("q") == n_quantiles).select("date", pl.col("ret").alias("top"))
    bot = qret.filter(pl.col("q") == 1).select("date", pl.col("ret").alias("bottom"))
    return top.join(bot, on="date", how="inner").with_columns(spread=pl.col("top") - pl.col("bottom")).sort("date")


def performance(ret: pl.Series, periods_per_year: float = 252.0, horizon: int = 1) -> dict:
    """Annualised stats of a per-date return series with holding horizon h (overlapping)."""
    r = ret.drop_nulls()
    if r.len() < 2:
        return {}
    per_year = periods_per_year / horizon
    mean, std = float(r.mean()), float(r.std())
    # overlapping h-period returns cannot be compounded day by day; sample every h-th observation
    r_no = r.gather(list(range(0, r.len(), horizon))) if horizon > 1 else r
    cum = (1 + r_no).cum_prod()
    dd = (cum / cum.cum_max() - 1).min()
    return {
        "ann_return": round(mean * per_year, 4),
        "ann_vol": round(std * math.sqrt(per_year), 4),
        "sharpe": round(mean / std * math.sqrt(per_year), 3) if std > 0 else float("nan"),
        "max_drawdown": round(float(dd), 4),
        "total_return": round(float(cum[-1]) - 1, 4),
        "hit_rate": round(float((r > 0).mean()), 3),
        "n": r.len(),
        "n_non_overlapping": r_no.len(),
    }


def quantile_summary(qret: pl.DataFrame, horizon: int = 1) -> pl.DataFrame:
    return (
        qret.group_by("q")
        .agg(mean_ret_bp=(pl.col("ret").mean() * 1e4).round(2), n_dates=pl.len(), names=pl.col("names").mean().round(0))
        .sort("q")
    )


def signal_autocorr(df: pl.DataFrame, signal: str = "signal", lags: tuple[int, ...] = (1, 5, 21)) -> pl.DataFrame:
    """Cross-sectional rank autocorrelation of the signal at several lags (persistence)."""
    d = (
        df.select("security_id", "date", signal)
        .sort(["security_id", "date"])
        .with_columns(r=pl.col(signal).rank().over("date"))
    )
    rows = []
    for k in lags:
        lagged = d.with_columns(r_lag=pl.col("r").shift(k).over("security_id")).filter(pl.col("r_lag").is_not_null())
        ac = lagged.group_by("date").agg(ac=pl.corr(pl.col("r"), pl.col("r_lag")), n=pl.len()).filter(pl.col("n") >= 20)
        rows.append({"lag": k, "rank_autocorr": round(float(ac["ac"].mean()), 3) if ac.height else None})
    return pl.DataFrame(rows)


def quantile_turnover(qret_members: pl.DataFrame, n_quantiles: int = 10) -> dict:
    """Share of top/bottom-quantile names replaced from one date to the next."""
    d = qret_members.select("security_id", "date", "q").sort(["security_id", "date"])
    out = {}
    for q, name in ((n_quantiles, "top"), (1, "bottom")):
        m = d.filter(pl.col("q") == q).with_columns(prev=pl.col("date").shift(1).over("security_id"))
        dates = m.select("date").unique().sort("date").with_columns(prev_date=pl.col("date").shift(1))
        m = m.join(dates, on="date", how="left")
        stayed = m.filter(pl.col("prev") == pl.col("prev_date")).group_by("date").len().rename({"len": "stayed"})
        total = m.group_by("date").len().rename({"len": "total"})
        t = (
            total.join(stayed, on="date", how="left")
            .filter(pl.col("date") > pl.col("date").min())
            .with_columns(turnover=1 - pl.col("stayed").fill_null(0) / pl.col("total"))
        )
        out[f"{name}_turnover"] = round(float(t["turnover"].mean()), 3) if t.height else None
    return out


def by_year(ic: pl.DataFrame) -> pl.DataFrame:
    return (
        ic.group_by(pl.col("date").dt.year().alias("year"))
        .agg(ic_mean=pl.col("ic").mean().round(4), hit=(pl.col("ic") > 0).mean().round(3), n=pl.len())
        .sort("year")
    )


def walk_forward_windows(dates: list, train_sessions: int, test_sessions: int, step: int | None = None) -> list[tuple]:
    """(train_start, train_end, test_start, test_end) rolling windows over a sorted date list."""
    step = step or test_sessions
    out = []
    i = 0
    while i + train_sessions + test_sessions <= len(dates):
        tr0, tr1 = dates[i], dates[i + train_sessions - 1]
        te0, te1 = dates[i + train_sessions], dates[i + train_sessions + test_sessions - 1]
        out.append((tr0, tr1, te0, te1))
        i += step
    return out
