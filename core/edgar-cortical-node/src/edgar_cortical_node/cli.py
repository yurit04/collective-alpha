"""`cortical` command line: load and refresh SEC EDGAR company facts, inspect them."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Annotated

import typer

from ._config import PLACEHOLDER_AGENT, ConfigError, Settings, load_settings
from ._layout import Layout

app = typer.Typer(help="edgar-cortical-node: point-in-time SEC fundamentals for the Collective.",
                  no_args_is_help=True, pretty_exceptions_show_locals=False)
service_app = typer.Typer(help="Daily sync service (macOS launchd).", no_args_is_help=True)
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
    data_root: Annotated[Path | None, typer.Option(help="Override the data root (default ~/market_data/edgar).")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    _state["data_root"] = data_root
    root = logging.getLogger("edgar_cortical_node")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not root.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        root.addHandler(h)
        try:
            layout = Layout(_settings().data_root)
            layout.logs_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(layout.logs_dir / "cortical.log")
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s"))
            root.addHandler(fh)
        except OSError:
            pass


def _report(result) -> None:
    typer.echo("  " + result.line())
    if not result.ok:
        typer.secho("Some items failed; they are retried on the next sync.", fg=typer.colors.YELLOW)
        raise typer.Exit(1)


@app.command()
def init() -> None:
    """Create folders and show which SEC contact is used."""
    s = _settings()
    Layout(s.data_root).ensure()
    typer.echo(f"Data root: {s.data_root}")
    if s.user_agent == PLACEHOLDER_AGENT:
        typer.echo("SEC contact: placeholder (accepted). To use your own, set user_agent under "
                   "[edgar_cortical_node] in your collective.toml.")
    else:
        typer.echo("SEC contact: set in config")
    typer.echo("Next: `cortical backfill` (1.4 GB download, then conversion).")


@app.command()
def backfill(
    force: Annotated[bool, typer.Option(help="Reload even if the bulk file has not changed.")] = False,
    download: Annotated[bool, typer.Option(help="Download the bulk file (else re-parse the stored one).")] = True,
) -> None:
    """Load every company's facts from the SEC's nightly bulk file."""
    from ._ingest import Ingestor

    _report(Ingestor(_settings()).bulk(force=force, download=download))


@app.command()
def profiles(
    force: Annotated[bool, typer.Option(help="Reload even if the file has not changed.")] = False,
    download: Annotated[bool, typer.Option(help="Download submissions.zip (else re-parse the stored one).")] = True,
) -> None:
    """Load company profiles and name histories from the SEC's bulk submissions file (1.6 GB)."""
    from ._ingest import Ingestor

    _report(Ingestor(_settings()).profiles(force=force, download=download))


@app.command()
def sync() -> None:
    """Re-fetch companies that filed since the last sync (full reload once a month)."""
    from ._ingest import Ingestor

    _report(Ingestor(_settings()).sync())


@app.command()
def series() -> None:
    """Rebuild the materialized series and share counts from the facts (also done by backfill
    and sync); reads are then instant instead of rebuilding from facts."""
    from ._ingest import Ingestor

    _report(Ingestor(_settings()).series())


@app.command()
def status() -> None:
    """Bulk file date, sync progress, number of companies, size on disk, whether the
    materialized series are current."""
    from ._store import EdgarStore

    info = EdgarStore(settings=_settings()).status()
    info["facts_size"] = f"{info.pop('facts_bytes', 0) / 1e9:.2f} GB"
    typer.echo(json.dumps(info, indent=2, default=str))


@app.command()
def show(
    cik: Annotated[str, typer.Argument(help="A CIK (with or without leading zeros).")],
    items: Annotated[str, typer.Option(help="Comma-separated items from CONCEPTS.")] = "revenue,net_income",
    rows: Annotated[int, typer.Option()] = 12,
) -> None:
    """Latest quarters of some items, with filing dates, for one company."""
    import polars as pl

    from ._store import EdgarStore

    store = EdgarStore(settings=_settings())
    try:
        s = store.series([i.strip() for i in items.split(",") if i.strip()], ciks=cik)
    except (ValueError, FileNotFoundError) as exc:
        _fail(str(exc))
    name = store.companies().filter(pl.col("cik") == f"{int(cik):010d}")["name"]
    typer.echo(name[0] if len(name) else f"CIK {cik}")
    with pl.Config(tbl_rows=rows * 4, tbl_width_chars=200):
        typer.echo(s.sort("item", "end").group_by("item", maintain_order=True).tail(rows)
                   .select("item", "period", "end", "value", "ttm", "filed", "source", "tag"))
    try:
        prof = store.profiles(cik)
        if prof.height:
            p = prof.row(0, named=True)
            typer.echo(f"SIC {p['sic']} {p['sic_description'] or ''}; fiscal year end {p['fiscal_year_end']}; "
                       f"tickers {', '.join(p['tickers']) or '-'}")
            former = store.names(cik).filter(pl.col("valid_to").is_not_null())
            for r in former.iter_rows(named=True):
                typer.echo(f"  formerly {r['name']} ({r['valid_from']} to {r['valid_to']})")
    except FileNotFoundError:
        pass
    sh = store.shares(cik)
    if sh.height:
        typer.echo(f"shares outstanding: {sh['shares'][-1]:,.0f} at {sh['end'][-1]} ({sh['tag'][-1]})")


@service_app.command("install")
def service_install() -> None:
    """Install the daily sync (Tue-Sat at sync_hour:sync_minute, default 07:00)."""
    from . import _services

    path = _services.install(_settings())
    typer.echo(f"installed {path}")


@service_app.command("remove")
def service_remove() -> None:
    from . import _services

    typer.echo("removed" if _services.remove() else "not installed")


@service_app.command("status")
def service_status() -> None:
    from . import _services

    typer.echo(_services.status())


if __name__ == "__main__":
    app()
