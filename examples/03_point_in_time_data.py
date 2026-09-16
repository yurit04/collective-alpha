"""Read the store the way research code should: keyed by security, point in time, survivorship free.

    uv run python examples/03_point_in_time_data.py

Shows the four joins that every study needs: ticker to security, universe membership on a date,
the daily panel, and features. Also demonstrates why tickers alone are not safe.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from collective_alpha.features.base import load_features
from collective_alpha.panel.build import load_panel
from collective_alpha.storage.catalog import Catalog
from collective_alpha.universe.builder import load_master
from collective_alpha.universe.security_master import map_to_security
from collective_alpha.universe.universes import load_universe

pl.Config.set_tbl_width_chars(200)


def why_tickers_are_not_ids() -> None:
    master = load_master()
    print("== the ticker FB has meant two different companies ==")
    print(
        master.filter(pl.col("ticker") == "FB").select(
            "ticker", "security_id", "valid_from", "valid_to", "name", "type"
        )
    )

    # attaching a security_id to any frame with (ticker, date)
    trades = pl.DataFrame(
        {"ticker": ["FB", "FB"], "date": [dt.date(2022, 1, 3), dt.date(2026, 1, 5)], "note": ["Meta", "an ETF"]}
    )
    print(map_to_security(trades, master).select("ticker", "date", "note", "security_id"))


def universe_membership() -> None:
    d = dt.date(2026, 9, 1)
    u = load_universe("liquid_1500").filter(pl.col("date") == d)
    print(f"\n== liquid_1500 on {d}: {u.height} members, ranked by trailing dollar volume ==")
    cat = Catalog()
    print(
        cat.sql(
            f"""select u.rank, m.ticker, m.name, round(u.lag_adv/1e6) adv_musd
                from universes u
                join security_master m
                  on m.security_id = u.security_id and u.date between m.valid_from and m.valid_to
                where u.name = 'liquid_1500' and u.date = DATE '{d}'
                order by u.rank limit 5"""
        ).pl()
    )


def panel_and_features() -> None:
    start = dt.date(2026, 6, 1)
    panel = load_panel(start=start, universe="liquid_1500", columns=["ticker", "close", "ret", "adj_close"])
    feats = load_features(["price", "intra"], start=start, universe="liquid_1500", columns=["mom_12_1", "spread_21d"])
    df = panel.join(feats, on=["security_id", "date"], how="inner")
    print(f"\n== panel joined to features: {df.height:,} rows, {df['security_id'].n_unique():,} securities ==")
    print(df.filter(pl.col("ticker") == "AAPL").tail(3))
    # prices are unadjusted; adj_close is the split-adjusted series, ret is the total return
    print("\nnever compute returns from `close` directly: use `ret` (total) or `adj_close` (price).")


def delisted_names_are_present() -> None:
    cat = Catalog()
    print("\n== the store keeps names that no longer trade, so studies are survivorship free ==")
    print(
        cat.sql(
            """select m.ticker, s.name, s.first_trade, s.last_trade
               from securities s join security_master m on m.security_id = s.security_id
               where m.ticker in ('TWTR','SIVB','ATVI') group by all order by s.last_trade"""
        ).pl()
    )


if __name__ == "__main__":
    why_tickers_are_not_ids()
    universe_membership()
    panel_and_features()
    delisted_names_are_present()
