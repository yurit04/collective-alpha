"""`vinculum` command line: build the Massive-EDGAR join, check it, look at companies and universes."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Annotated

import typer

from ._config import ConfigError, Settings, load_settings
from ._layout import Layout

app = typer.Typer(help="vinculum: market cap, size universes and point-in-time fundamentals per security.",
                  no_args_is_help=True, pretty_exceptions_show_locals=False)
service_app = typer.Typer(help="Nightly build service (macOS launchd).", no_args_is_help=True)
app.add_typer(service_app, name="service")
_state: dict = {}


def _settings() -> Settings:
    if "settings" not in _state:
        try:
            overrides = {"data_root": _state["data_root"]} if _state.get("data_root") else {}
            _state["settings"] = load_settings(**overrides)
        except ConfigError as exc:
            _fail(str(exc))
    return _state["settings"]


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _vinculum():
    from ._store import Vinculum
    return Vinculum(settings=_settings())


@app.callback()
def main(
    data_root: Annotated[Path | None, typer.Option(help="Override the data root (default ~/market_data/vinculum).")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    _state["data_root"] = data_root
    root = logging.getLogger("vinculum")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not root.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        root.addHandler(h)
        try:
            layout = Layout(_settings().data_root)
            layout.logs_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(layout.logs_dir / "vinculum.log")
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s"))
            root.addHandler(fh)
        except OSError:
            pass


@app.command()
def build(
    years: Annotated[str | None, typer.Option(help="Only rewrite these market-cap years, e.g. 2024,2025.")] = None,
) -> None:
    """Rebuild the CIK map, share counts, basis ratios and the daily market-cap table."""
    v = _vinculum()
    try:
        info = v.build([int(y) for y in years.split(",")] if years else None)
    except FileNotFoundError as exc:
        _fail(str(exc))
    typer.echo(f"  built {info['rows']:,} rows ({info['years'][0]}-{info['years'][1]}) in {info['seconds']}s")
    typer.echo(f"  CIKs: {info['cik_status']}")
    typer.echo(f"  basis: {info['basis']}")
    typer.echo(f"  penny ADRs withheld: {info['penny_adr_days']:,} security-days")


@app.command()
def check() -> None:
    """Known market caps, class handling, top companies, agreement with Massive, coverage."""
    results = _vinculum().check()
    for name, ok, detail in results:
        typer.secho(f"  {'pass' if ok else 'FAIL'}    {name}  ({detail})",
                    fg=None if ok else typer.colors.RED)
    if not all(ok for _, ok, _ in results):
        raise typer.Exit(1)


@app.command()
def status() -> None:
    """When it was built, from what, and what it found."""
    info = _vinculum().status()
    info["market_cap_size"] = f"{info.pop('market_cap_bytes', 0) / 1e9:.2f} GB"
    typer.echo(json.dumps(info, indent=2, default=str))


@app.command()
def show(
    ticker: Annotated[str, typer.Argument(help="A ticker (as of today) or a security id.")],
    rows: Annotated[int, typer.Option(help="Month ends to show.")] = 12,
) -> None:
    """A company's CIK history, basis ratio and recent month-end market caps."""
    import polars as pl

    v = _vinculum()
    try:
        mc = v.market_cap(ticker, by="security")
    except FileNotFoundError as exc:
        _fail(str(exc))
    if mc.is_empty():
        _fail(f"no rows for {ticker}")
    sid = mc["security_id"][0]
    typer.echo(f"{ticker} = {sid}")
    with pl.Config(tbl_rows=50, tbl_width_chars=200, fmt_str_lengths=40):
        typer.echo(v.cik_map(sid).select("valid_from", "valid_to", "cik", "status", "massive_name", "edgar_name"))
        typer.echo(v.basis(sid).select("type", "basis_ratio", "basis", "massive_shares", "sec_shares"))
        month = mc.with_columns(pl.col("date").dt.strftime("%Y-%m").alias("m")).group_by("m").agg(pl.all().last())
        typer.echo(month.sort("date").tail(rows).select("date", "ticker", "close", "shares", "market_cap",
                                                         "issuer_market_cap", "is_primary", "shares_end",
                                                         "float_fraction", "float_date"))


@app.command()
def universe(
    on: Annotated[str, typer.Argument(help="Date (YYYY-MM-DD).")],
    top: Annotated[int | None, typer.Option(help="The N largest companies.")] = None,
    size: Annotated[str | None, typer.Option(help="large, mid, small or micro (comma-separated).")] = None,
    by: Annotated[str, typer.Option(help="market_cap or float_market_cap.")] = "market_cap",
    rows: Annotated[int, typer.Option()] = 20,
) -> None:
    """A size universe on a day (ranked at the previous month end)."""
    import polars as pl

    try:
        u = _vinculum().universe(on, top=top, size=size.split(",") if size else None, by=by)
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
    typer.echo(f"{u.height:,} companies (ranked on {u['rank_date'][0] if u.height else '-'})")
    with pl.Config(tbl_rows=rows, tbl_width_chars=160):
        typer.echo(u.head(rows).select("rank", "size", "ticker", "rank_market_cap", "market_cap", "float_fraction",
                                       "close"))


@service_app.command("install")
def service_install() -> None:
    """Install and start the nightly build (Tue-Sat 07:45, after both data syncs)."""
    from . import _services

    try:
        path = _services.install(_settings())
    except RuntimeError as exc:
        _fail(str(exc))
    s = _settings()
    typer.echo(f"Installed {path} (Tue-Sat {s.build_hour:02d}:{s.build_minute:02d}).")


@service_app.command("remove")
def service_remove() -> None:
    """Stop and remove the nightly build."""
    from . import _services

    typer.echo("Removed." if _services.remove() else "Not installed.")


@service_app.command("status")
def service_status() -> None:
    """Is the nightly build installed and loaded."""
    from . import _services

    typer.echo(json.dumps(_services.status(), indent=2))
