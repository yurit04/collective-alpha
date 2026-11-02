"""`sensor` command line: download FINRA short interest and short sale volume, inspect them."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Annotated

import typer

from ._config import ConfigError, Settings, load_settings
from ._layout import Layout

app = typer.Typer(help="finra-sensor-array: FINRA short interest and short sale volume for the Collective.",
                  no_args_is_help=True, pretty_exceptions_show_locals=False)
backfill_app = typer.Typer(help="Download history.", no_args_is_help=True)
service_app = typer.Typer(help="Daily sync service (macOS launchd).", no_args_is_help=True)
app.add_typer(backfill_app, name="backfill")
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


@app.callback()
def main(
    data_root: Annotated[Path | None, typer.Option(help="Override the data root (default ~/market_data/finra).")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    _state["data_root"] = data_root
    root = logging.getLogger("finra_sensor_array")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not root.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        root.addHandler(h)
        try:
            layout = Layout(_settings().data_root)
            layout.logs_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(layout.logs_dir / "sensor.log")
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s"))
            root.addHandler(fh)
        except OSError:
            pass


def _ingestor():
    from ._ingest import Ingestor

    return Ingestor(_settings())


def _report(results) -> None:
    failed = False
    for r in results:
        typer.echo("  " + r.line())
        failed = failed or not r.ok
    if failed:
        typer.secho("Some days failed; they are tried again on the next run.", fg=typer.colors.YELLOW)
        raise typer.Exit(1)


@backfill_app.command("short-interest")
def backfill_short_interest(
    start: Annotated[str | None, typer.Option(help="First date (default 2017-12-01).")] = None,
    end: Annotated[str | None, typer.Option(help="Last date (default today).")] = None,
) -> None:
    """Short interest for every settlement date (about 2,300 small requests for full history)."""
    _report([_ingestor().short_interest(start, end)])


@backfill_app.command("short-volume")
def backfill_short_volume(
    start: Annotated[str | None, typer.Option(help="First date (default 2009-08-03).")] = None,
    end: Annotated[str | None, typer.Option(help="Last date (default today).")] = None,
) -> None:
    """Daily short sale volume (full history: about 9,000 files, 2-3 GB downloaded)."""
    _report([_ingestor().short_volume(start, end)])


@app.command()
def sync() -> None:
    """Daily catch-up of both datasets (re-fetches recent days for FINRA's revisions)."""
    _report(_ingestor().sync())


@app.command()
def status() -> None:
    """What is stored."""
    from ._store import FinraStore

    typer.echo(json.dumps(FinraStore(settings=_settings()).status(), indent=2, default=str))


@app.command()
def show(
    ticker: Annotated[str, typer.Argument(help="A FINRA symbol or a ticker (BRK.B works).")],
    rows: Annotated[int, typer.Option()] = 6,
) -> None:
    """The latest short interest and short sale volume for a symbol."""
    import polars as pl

    from ._store import FinraStore

    store = FinraStore(settings=_settings())
    with pl.Config(tbl_rows=rows * 2, tbl_width_chars=160):
        for name, cols in (("short_interest", ["settlement_date", "published_date", "symbol", "market",
                                               "short_interest", "avg_daily_volume", "days_to_cover"]),
                           ("short_volume", ["date", "symbol", "short_volume", "total_volume", "markets"])):
            try:
                df = getattr(store, name)(ticker).select(cols).tail(rows)
            except FileNotFoundError as exc:
                typer.echo(f"{name}: {exc}")
                continue
            typer.echo(f"{name}:\n{df}")


@service_app.command("install")
def service_install() -> None:
    """Install the daily sync (Tue-Sat 07:15 local)."""
    from . import _services

    typer.secho(f"installed: {_services.install(_settings())}", fg=typer.colors.GREEN)


@service_app.command("remove")
def service_remove() -> None:
    from . import _services

    typer.echo("removed" if _services.remove() else "not installed")


@service_app.command("status")
def service_status() -> None:
    from . import _services

    typer.echo(json.dumps(_services.status(), indent=2))
