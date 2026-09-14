"""Forward returns aligned to a signal date D: what a position opened at D's close earns.

fwd_ret_{h}d   close(D) -> close(D+h sessions), total return (tr_index based)
fwd_o2c_1d     open(D+1) -> close(D+1): what you earn if you can only trade the next open
fwd_c2o_1d     close(D) -> open(D+1): the overnight part

If a security stops trading inside the horizon the return is compounded to its last bar and then
with `delist_return` (default 0: you get the last price). Rows within h sessions of the store's end
are null (unknown future), not truncated.
"""

from __future__ import annotations

import polars as pl

HORIZONS = (1, 2, 3, 5, 10, 21, 42, 63)


def forward_returns(
    panel: pl.DataFrame,
    horizons: tuple[int, ...] = HORIZONS,
    delist_return: float = 0.0,
    sessions_end_idx: int | None = None,
) -> pl.DataFrame:
    """panel: security_id, date, sidx? (session index), tr_index, open, close, is_last_trade.
    Returns (security_id, date, fwd_ret_{h}d..., fwd_o2c_1d, fwd_c2o_1d)."""
    p = panel.sort(["security_id", "date"])
    if "sidx" not in p.columns:
        # session index from the union of dates in the panel (dense calendar of observed sessions)
        cal = p.select("date").unique().sort("date").with_row_index("sidx")
        p = p.join(cal, on="date", how="left")
    end_idx = sessions_end_idx if sessions_end_idx is not None else int(p["sidx"].max())
    log_tr = pl.col("tr_index").log()
    n = pl.len().over("security_id")
    row = pl.int_range(0, pl.len()).over("security_id")
    last_sidx = pl.col("sidx").max().over("security_id")
    last_log_tr = log_tr.last().over("security_id")
    exprs = []
    for h in horizons:
        ahead = log_tr.shift(-h).over("security_id")  # row h bars ahead (may be > h sessions if gaps)
        sidx_ahead = pl.col("sidx").shift(-h).over("security_id")
        # regular case: h rows ahead exist and are within ~h sessions (allow small gaps up to 2h)
        regular = (ahead - log_tr).exp() - 1
        # delisting case: fewer than h rows remain and the security's last bar is before the store end
        delisted = (row + h >= n) & (last_sidx + 1 <= end_idx)
        # data end: not enough future sessions in the store
        unknown = (pl.col("sidx") + h > end_idx) & ~delisted
        delist_ret = (last_log_tr - log_tr).exp() * (1 + delist_return) - 1
        exprs.append(
            pl.when(unknown)
            .then(None)
            .when(delisted)
            .then(delist_ret)
            .when(sidx_ahead - pl.col("sidx") <= 2 * h)
            .then(regular)
            .otherwise(None)
            .alias(f"fwd_ret_{h}d")
        )
    nxt_open = pl.col("open").shift(-1).over("security_id")
    nxt_close = pl.col("close").shift(-1).over("security_id")
    nxt_sidx = pl.col("sidx").shift(-1).over("security_id")
    adjacent = (nxt_sidx - pl.col("sidx")) <= 2
    exprs += [
        pl.when(adjacent).then(nxt_close / nxt_open - 1).otherwise(None).alias("fwd_o2c_1d"),
        pl.when(adjacent)
        .then(nxt_open / pl.col("close") * pl.col("split_ratio").shift(-1).over("security_id") - 1)
        .otherwise(None)
        .alias("fwd_c2o_1d"),
    ]
    return p.with_columns(exprs).select(
        ["security_id", "date", *[f"fwd_ret_{h}d" for h in horizons], "fwd_o2c_1d", "fwd_c2o_1d"]
    )
