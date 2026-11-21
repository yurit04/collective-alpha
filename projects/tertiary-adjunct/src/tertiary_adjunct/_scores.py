"""Signal panel on the signal dates, month-end ICs, and the three composites (C1, C2, C3)."""

from __future__ import annotations

from datetime import date
from typing import Any

import polars as pl
from neural_processor import SIGNALS, composite

from ._protocol import signals


def signal_panel(processor, protocol: dict[str, Any], dates: list[date]) -> pl.DataFrame:
    """Point-in-time signals of every protocol signal for the top N on each signal date. Built a
    calendar year at a time from the daily panel (the universe ranked on each date), keeping
    only the signal dates."""
    sigs = signals(protocol)
    top = protocol["universe"]["top"]
    want = set(dates)
    parts = []
    for year in sorted({d.year for d in dates}):
        ds = [d for d in dates if d.year == year]
        p = processor.panel(sigs, start=ds[0], end=ds[-1], top=top, freq="1d")
        parts.append(p.filter(pl.col("date").is_in(list(want))))
    return pl.concat(parts, how="diagonal_relaxed").sort("date", "security_id")


def month_end_ics(engine, panel: pl.DataFrame, sigs: list[str], month_ends: list[date], lag: int = 1,
                  min_names: int = 50) -> pl.DataFrame:
    """Rank IC per month end and signal (oriented by its direction) against the return from
    that month end (+lag) to the next one (+lag). Columns: date, next_date, signal, ic. An IC
    is known once next_date has passed."""
    me = sorted(month_ends)
    p = panel.filter(pl.col("date").is_in(me)).select("date", "security_id", *sigs)
    f = engine.forward_returns(p, lag)
    nxt = {d: me[i + 1] for i, d in enumerate(me[:-1])}
    rows = []
    for s in sigs:
        sign = SIGNALS[s].direction if s in SIGNALS else 1
        g = (f.filter(pl.col(s).is_not_null() & pl.col("ret_fwd").is_not_null())
             .group_by("date").agg(pl.corr(pl.col(s) * sign, "ret_fwd", method="spearman").alias("ic"),
                                   pl.len().alias("n"))
             .filter(pl.col("n") >= min_names).with_columns(pl.lit(s).alias("signal")))
        rows.append(g.select("date", "signal", "ic"))
    out = pl.concat(rows).filter(pl.col("date").is_in(list(nxt)))
    return (out.with_columns(pl.col("date").replace_strict(nxt, return_dtype=pl.Date).alias("next_date"))
            .select("date", "next_date", "signal", "ic").sort("date", "signal"))


def walk_forward_weights(ics: pl.DataFrame, dates: list[date], sigs: list[str], window_months: int,
                         min_months: int, shrink: float) -> pl.DataFrame:
    """Per signal date: each signal's weight from the ICs known strictly before it (next_date <
    date), the last `window_months` of them: max(0, mean / std), normalized to 1, then
    (1 - shrink) x that + shrink x equal. Fewer than `min_months` known ICs, or no positive
    one: equal weights. Columns: date, signal, weight, known (how many ICs were used)."""
    eq = 1.0 / len(sigs)
    ic = ics.filter(pl.col("signal").is_in(sigs)).sort("date")
    rows = []
    for d in dates:
        known = ic.filter(pl.col("next_date") < d)
        months = sorted(known["date"].unique().to_list())[-window_months:]
        w = {s: eq for s in sigs}
        if len(months) >= min_months:
            k = known.filter(pl.col("date").is_in(months)).group_by("signal").agg(
                pl.col("ic").mean().alias("m"), pl.col("ic").std().alias("sd"))
            ir = {r["signal"]: max(0.0, r["m"] / r["sd"]) if r["sd"] and r["sd"] > 0 else 0.0
                  for r in k.iter_rows(named=True)}
            total = sum(ir.get(s, 0.0) for s in sigs)
            if total > 0:
                w = {s: (1 - shrink) * ir.get(s, 0.0) / total + shrink * eq for s in sigs}
        rows += [{"date": d, "signal": s, "weight": w[s], "known": len(months)} for s in sigs]
    return pl.DataFrame(rows, schema={"date": pl.Date, "signal": pl.String, "weight": pl.Float64,
                                      "known": pl.Int64})


def _group_of(protocol: dict[str, Any]) -> dict[str, str]:
    return {s: g for g, ss in protocol["signals"].items() for s in ss}


def equal_groups(panel: pl.DataFrame, protocol: dict[str, Any], groups: list[str], name: str) -> pl.DataFrame:
    """Each group the mean of its signals' oriented z-scores (2 needed where it has 2 or more),
    then the score the mean of the groups (2 needed)."""
    g = protocol["signals"]
    out = panel
    for grp in groups:
        out = composite(out, g[grp], name=f"__g_{grp}", min_signals=min(2, len(g[grp])))
    out = composite(out, {f"__g_{grp}": 1.0 for grp in groups}, name=name, min_signals=2)
    return out.drop([f"__g_{grp}" for grp in groups])


def weighted_signals(panel: pl.DataFrame, protocol: dict[str, Any], weights: pl.DataFrame, name: str) -> pl.DataFrame:
    """Per date, the weighted mean of the signals' oriented z-scores with that date's weights,
    over the signals a row has (reweighted). Null unless the row has positively weighted
    signals from 2 groups."""
    sigs = weights["signal"].unique(maintain_order=True).to_list()
    groups = _group_of(protocol)
    z = panel.select("date", "security_id", *sigs)
    for s in sigs:                                               # oriented, winsorized z-score per date
        z = composite(z, [s], name=f"__z_{s}")
    long = (z.select("date", "security_id", *[f"__z_{s}" for s in sigs])
            .unpivot(index=["date", "security_id"], variable_name="signal", value_name="z")
            .with_columns(pl.col("signal").str.strip_prefix("__z_"))
            .drop_nulls("z")
            .join(weights.select("date", "signal", "weight"), on=["date", "signal"])
            .filter(pl.col("weight") > 0)
            .with_columns(pl.col("signal").replace_strict(groups, default=None).alias("group")))
    score = (long.group_by("date", "security_id")
             .agg(((pl.col("z") * pl.col("weight")).sum() / pl.col("weight").sum()).alias(name),
                  pl.col("group").n_unique().alias("__groups"))
             .filter(pl.col("__groups") >= 2).drop("__groups"))
    return panel.join(score, on=["date", "security_id"], how="left")


def scores(panel: pl.DataFrame, protocol: dict[str, Any], ics: pl.DataFrame | None = None) -> pl.DataFrame:
    """The panel with one column per protocol composite (C1, C2, C3). C3 needs the month-end
    ICs (month_end_ics); without them it is left out."""
    out = panel
    dates = sorted(panel["date"].unique().to_list())
    for name, spec in protocol["composites"].items():
        if spec["rule"] == "equal_groups":
            out = equal_groups(out, protocol, spec["groups"], name)
        elif spec["rule"] == "walk_forward_ic":
            if ics is None:
                continue
            w = walk_forward_weights(ics, dates, signals(protocol, spec["groups"]), spec["window_months"],
                                     spec["min_months"], spec["shrink"])
            out = weighted_signals(out, protocol, w, name)
        else:
            raise ValueError(f"unknown composite rule {spec['rule']!r}")
    return out


def score_names(protocol: dict[str, Any]) -> list[str]:
    return list(protocol["composites"])


__all__ = ["equal_groups", "month_end_ics", "score_names", "scores", "signal_panel", "walk_forward_weights",
           "weighted_signals"]
