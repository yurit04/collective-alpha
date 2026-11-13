"""`adapt` command line: measure transaction costs from Massive trades and quotes, inspect them."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Annotated

import polars as pl
import typer

from ._config import ConfigError, Settings, load_settings
from ._layout import BusyError, Layout

app = typer.Typer(help="adaptation-matrix: transaction costs measured from trades and quotes.",
                  no_args_is_help=True, pretty_exceptions_show_locals=False)
_state: dict = {}


def _settings() -> Settings:
    if "settings" not in _state:
        try:
            overrides = {"data_root": _state["data_root"]} if _state.get("data_root") else {}
            _state["settings"] = load_settings(**overrides)
        except ConfigError as exc:
            _fail(str(exc))
    return _state["settings"]


def _fail(message: str, code: int = 1) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


def _matrix():
    from ._store import AdaptationMatrix

    return AdaptationMatrix(settings=_settings())


@app.callback()
def main(
    data_root: Annotated[Path | None, typer.Option(help="Override the data root (default ~/market_data/adaptation_matrix).")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    _state["data_root"] = data_root
    root = logging.getLogger("adaptation_matrix")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not root.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        root.addHandler(h)


def _file_log() -> None:
    layout = Layout(_settings().data_root)
    layout.logs_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(layout.logs_dir / "adapt.log")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s"))
    logging.getLogger("adaptation_matrix").addHandler(fh)


@app.command()
def plan(
    end: Annotated[str | None, typer.Option(help="Last day (default yesterday).")] = None,
    recent_months: Annotated[int | None, typer.Option(help="Every-day window (default: the setting, 6).")] = None,
    mb_per_s: Annotated[float, typer.Option(help="Download speed for the time estimate.")] = 15.0,
    days: Annotated[bool, typer.Option("--days", help="List every day.")] = False,
) -> None:
    """The days a full run measures, and what downloading them takes (lists files only)."""
    m = _matrix()
    sched = m.schedule(end, recent_months)
    typer.echo(f"{sched.height} days: " + ", ".join(f"{s} {n}" for s, n in sched.group_by("set").len().sort("set").iter_rows())
               + f"; {sched['done'].sum()} done")
    est = m.estimate(sched, mb_per_s)
    with pl.Config(tbl_rows=100):
        typer.echo(est.group_by("set").agg(pl.col("days").sum(), pl.col("gb").sum().round(0), pl.col("hours").sum().round(1)).sort("set"))
    typer.echo(f"total: {est['gb'].sum():,.0f} GB to download, about {est['hours'].sum():,.0f} h at {mb_per_s:.0f} MB/s")
    if days:
        with pl.Config(tbl_rows=-1):
            typer.echo(sched)


@app.command()
def run(
    end: Annotated[str | None, typer.Option(help="Last day of the schedule (default yesterday).")] = None,
    sets: Annotated[str | None, typer.Option(help="Comma-separated sets: recent, sample, event.")] = None,
    limit: Annotated[int | None, typer.Option(help="Measure at most this many new days.")] = None,
    day: Annotated[list[str] | None, typer.Option("--day", help="Measure exactly these days (repeatable).")] = None,
) -> None:
    """Download, measure and keep scheduled days (long: ~15 minutes per recent day). Resumable."""
    _file_log()
    m = _matrix()
    try:
        res = m.run(end=end, sets=sets.split(",") if sets else None, limit=limit, days=day)
    except BusyError as exc:
        _fail(str(exc), 2)
    typer.echo(res.line())
    for d, err in res.failed.items():
        typer.secho(f"  {d}: {err}", fg=typer.colors.YELLOW)
    if res.failed:
        raise typer.Exit(1)


@app.command("measure-stored")
def measure_stored(day: str) -> None:
    """Measure a day whose full trades and quotes are already in the Massive store (no download)."""
    _file_log()
    try:
        rec = _matrix().measure_stored(day)
    except BusyError as exc:
        _fail(str(exc), 2)
    typer.echo(json.dumps(rec, indent=1, default=str))


@app.command()
def calibrate(test_start: Annotated[str | None, typer.Option(help="hold out days from here (default: the last 6 months)")] = None,
              refresh: Annotated[bool, typer.Option(help="rebuild the inputs (Engine.returns per measured day)")] = False
              ) -> None:
    """Fit the cost models on the measured days and print them (decision 0017): the spread formula
    and its out-of-sample check, the close, impact by size and the time-of-day profile."""
    import polars as pl

    cal = _matrix().calibrate(test_start=test_start, refresh=refresh)
    s = cal.spread
    with pl.Config(tbl_rows=40, float_precision=2, tbl_cols=20, tbl_width_chars=200, tbl_hide_dataframe_shape=True):
        typer.echo(f"spread fit ({s.rows:,} stock-days): adv_slope {s.adv_slope:.4f}, vol_slope {s.vol_slope:.4f}, "
                   f"knots {[(f'{k:.0e}', round(c, 4)) for k, c in s.knots]}")
        typer.echo("levels (bps at $100M ADV, 2% vol): "
                   + ", ".join(f"{y}: {v:.3f}" for y, v in s.levels.items()))
        typer.echo(f"\nheld out from {cal.test_start} (fit on earlier days):")
        typer.echo(cal.validation)
        typer.echo("\nclose vs the pre-close mid:")
        typer.echo(cal.close)
        typer.echo("\nimpact (square-root coefficient):")
        typer.echo(cal.impact)
        typer.echo(f"\ntime of day (tactical-cube tod): {cal.tod_pairs()}")
    try:
        from unimatrix_zero import SPREAD_FIT
        same = (abs(SPREAD_FIT.adv_slope - s.adv_slope) < 1e-3 and abs(SPREAD_FIT.vol_slope - s.vol_slope) < 1e-3
                and all(abs(dict(SPREAD_FIT.levels).get(y, 0) - v) < 1e-2 for y, v in s.levels.items()))
        typer.echo("\nunimatrix_zero.SPREAD_FIT " + ("matches this fit" if same else
                                                     "differs from this fit: update it (decision 0017)"))
    except ImportError:
        pass


@app.command()
def status() -> None:
    """Days measured, store size, failures."""
    m = _matrix()
    typer.echo(json.dumps(m.status(), indent=1, default=str))


@app.command()
def show(ticker: str, start: Annotated[str | None, typer.Option()] = None,
         end: Annotated[str | None, typer.Option()] = None) -> None:
    """Daily cost measures of one ticker."""
    cols = ["date", "ticker", "qhs_bps", "eff_bps", "rs_5m_bps", "pi_5m_bps", "eff_lit_bps", "eff_trf_bps",
            "close_vs_mid_bps", "qhs_preclose_bps", "dollar_volume", "outside_share"]
    df = _matrix().daily(ticker, start, end)
    with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=200):
        typer.echo(df.select([c for c in cols if c in df.columns]).sort("date"))
