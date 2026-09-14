import datetime as dt

import polars as pl

from collective_alpha.calendar import trading_days
from collective_alpha.universe.universes import (
    UniverseSpec,
    build_universe,
    lagged_stats,
    rebalance_dates,
    universe_stats,
)

D = dt.date
SESSIONS = trading_days(D(2023, 1, 3), D(2023, 6, 30))


def _panel(rows: dict[str, tuple[float, float, D | None, D | None]]) -> pl.DataFrame:
    """rows: security_id -> (close, volume, first_date, last_date)"""
    parts = []
    for sid, (close, vol, first, last) in rows.items():
        days = [d for d in SESSIONS if (first is None or d >= first) and (last is None or d <= last)]
        parts.append(
            pl.DataFrame(
                {
                    "security_id": [sid] * len(days),
                    "date": days,
                    "close": [close] * len(days),
                    "volume": [vol] * len(days),
                }
            )
        )
    return pl.concat(parts)


def _attrs(rows: dict[str, tuple[str, str, str | None]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "security_id": list(rows),
            "asof": [D(2022, 12, 1)] * len(rows),
            "type": [v[0] for v in rows.values()],
            "primary_exchange": [v[1] for v in rows.values()],
            "cik": [v[2] for v in rows.values()],
        }
    )


def test_rebalance_dates_monthly():
    rd = rebalance_dates(SESSIONS, "monthly")
    assert rd[:3] == [D(2023, 1, 3), D(2023, 2, 1), D(2023, 3, 1)]


def test_lagged_stats_use_previous_session_only():
    panel = _panel({"A": (10.0, 1000.0, None, None)})
    st = lagged_stats(panel, adv_window=5)
    row = st.filter(pl.col("date") == SESSIONS[6]).row(0, named=True)
    assert row["lag_date"] == SESSIONS[5]
    assert row["lag_hist"] == 6
    assert abs(row["lag_adv"] - 10_000.0) < 1e-9
    # the first session has no lag information at all
    assert st.filter(pl.col("date") == SESSIONS[0])["lag_close"][0] is None


def test_build_universe_rules():
    panel = _panel(
        {
            "BIG": (50.0, 1_000_000.0, None, None),  # adv 50M
            "MID": (20.0, 100_000.0, None, None),  # adv 2M
            "SMALL": (20.0, 1_000.0, None, None),  # adv 20k -> fails min_adv
            "PENNY": (0.5, 10_000_000.0, None, None),  # fails min_price
            "ETF": (100.0, 1_000_000.0, None, None),  # wrong type
            "DEAD": (30.0, 500_000.0, None, D(2023, 3, 15)),  # delists mid-March
            "NEWCO": (30.0, 500_000.0, D(2023, 4, 3), None),  # lists in April -> needs history
            "CLASSB": (20.0, 90_000.0, None, None),  # same issuer as MID, less liquid
        }
    )
    attrs = _attrs(
        {
            "BIG": ("CS", "XNYS", "1"),
            "MID": ("CS", "XNAS", "2"),
            "SMALL": ("CS", "XNAS", "3"),
            "PENNY": ("CS", "XNAS", "4"),
            "ETF": ("ETF", "ARCX", None),
            "DEAD": ("CS", "XNYS", "5"),
            "NEWCO": ("CS", "XNAS", "6"),
            "CLASSB": ("CS", "XNAS", "2"),
        }
    )
    last_trade = pl.DataFrame({"security_id": ["DEAD"], "last_trade": [D(2023, 3, 15)]})
    spec = UniverseSpec(
        "t", min_price=5.0, min_adv=100_000.0, adv_window=5, min_history_days=10, one_class_per_issuer=True
    )
    u = build_universe(spec, panel, attrs, last_trade, SESSIONS)

    feb = set(u.filter(pl.col("date") == D(2023, 2, 1))["security_id"].to_list())
    assert feb == {"BIG", "MID", "DEAD"}  # SMALL/PENNY/ETF excluded; CLASSB collapsed into MID's issuer
    # January rebalance has no history yet -> nobody qualifies
    assert u.filter(pl.col("date") == D(2023, 1, 3)).height == 0
    # DEAD drops out the day after its last trade, without waiting for the next rebalance
    assert "DEAD" in u.filter(pl.col("date") == D(2023, 3, 15))["security_id"].to_list()
    assert "DEAD" not in u.filter(pl.col("date") == D(2023, 3, 16))["security_id"].to_list()
    assert "DEAD" not in u.filter(pl.col("date") == D(2023, 4, 3))["security_id"].to_list()
    # NEWCO lists on the April rebalance date -> no history that day; 19 sessions by May -> joins in May
    assert "NEWCO" not in u.filter(pl.col("date") == D(2023, 4, 3))["security_id"].to_list()
    assert "NEWCO" in u.filter(pl.col("date") == D(2023, 5, 1))["security_id"].to_list()
    # ranks are by lagged ADV
    jun = u.filter(pl.col("date") == D(2023, 6, 1)).sort("rank")
    assert jun["security_id"].to_list()[:2] == ["BIG", "NEWCO"]


def test_entry_exit_bands():
    # 4 names; top_n=2, exit_n=3. Ranks by ADV: A > B > C > D initially; then C overtakes B slightly.
    base = {"A": 400.0, "B": 300.0, "C": 250.0, "D": 100.0}
    parts = []
    for sid, vol in base.items():
        vols = []
        for d in SESSIONS:
            v = vol
            if sid == "C" and d >= D(2023, 3, 1):
                v = 320.0  # C moves to rank 2, B to rank 3 (still within exit band)
            vols.append(v * 1000)
        parts.append(
            pl.DataFrame(
                {
                    "security_id": [sid] * len(SESSIONS),
                    "date": SESSIONS,
                    "close": [10.0] * len(SESSIONS),
                    "volume": vols,
                }
            )
        )
    panel = pl.concat(parts)
    attrs = _attrs({k: ("CS", "XNYS", k) for k in base})
    spec = UniverseSpec("t", min_price=1.0, min_adv=1.0, adv_window=5, min_history_days=5, top_n=2, exit_n=3)
    u = build_universe(
        spec,
        panel,
        attrs,
        pl.DataFrame({"security_id": [], "last_trade": []}, schema={"security_id": pl.Utf8, "last_trade": pl.Date}),
        SESSIONS,
    )
    feb = set(u.filter(pl.col("date") == D(2023, 2, 1))["security_id"].to_list())
    assert feb == {"A", "B"}
    # April rebalance: ranks A, C, B, D. B stays (rank 3 <= exit 3); C does not enter (rank 2 but top_n reached? no: entry is rank<=top_n) -> C enters
    apr = set(u.filter(pl.col("date") == D(2023, 4, 3))["security_id"].to_list())
    assert apr == {"A", "B", "C"}
    st = universe_stats(u)
    assert st.filter(pl.col("rebalance_date") == D(2023, 4, 3))["entries"][0] == 1


def test_no_look_ahead():
    """Changing bars on the rebalance date itself must not change membership on that date."""
    panel = _panel({"A": (10.0, 100_000.0, None, None), "B": (10.0, 50_000.0, None, None)})
    attrs = _attrs({"A": ("CS", "XNYS", "1"), "B": ("CS", "XNYS", "2")})
    lt = pl.DataFrame({"security_id": [], "last_trade": []}, schema={"security_id": pl.Utf8, "last_trade": pl.Date})
    spec = UniverseSpec("t", min_price=1.0, min_adv=600_000.0, adv_window=5, min_history_days=5)
    rd = D(2023, 3, 1)
    u1 = build_universe(spec, panel, attrs, lt, SESSIONS)
    # pump B's volume on the rebalance date only
    pumped = panel.with_columns(
        volume=pl.when((pl.col("security_id") == "B") & (pl.col("date") == rd)).then(1e9).otherwise(pl.col("volume"))
    )
    u2 = build_universe(spec, pumped, attrs, lt, SESSIONS)
    m1 = set(u1.filter(pl.col("date") == rd)["security_id"].to_list())
    m2 = set(u2.filter(pl.col("date") == rd)["security_id"].to_list())
    assert m1 == m2 == {"A"}


def test_security_panel_dedupes_same_day_rows():
    from collective_alpha.universe.universes import security_panel

    master = pl.DataFrame(
        {
            "ticker": ["OLD", "NEW"],
            "security_id": ["S1", "S1"],
            "valid_from": [D(2023, 1, 3), D(2023, 1, 3)],
            "valid_to": [D(2023, 6, 30), D(2023, 6, 30)],
        }
    )
    bars = pl.DataFrame(
        {
            "ticker": ["OLD", "NEW", "NEW"],
            "date": [D(2023, 2, 1), D(2023, 2, 1), D(2023, 2, 2)],
            "close": [10.0, 10.1, 10.2],
            "volume": [100.0, 900.0, 500.0],
        }
    )
    p = security_panel(bars, master)
    assert p.height == 2
    feb1 = p.filter(pl.col("date") == D(2023, 2, 1)).row(0, named=True)
    assert feb1["ticker"] == "NEW" and feb1["volume"] == 900.0  # max-volume row kept, not summed
