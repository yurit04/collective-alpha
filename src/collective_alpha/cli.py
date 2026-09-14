"""`ca` command line: probe entitlements, sync datasets, inspect status."""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Annotated

import polars as pl
import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from collective_alpha.config import get_settings

app = typer.Typer(help="collective-alpha data platform", no_args_is_help=True)
sync_app = typer.Typer(help="Sync datasets from Massive", no_args_is_help=True)
app.add_typer(sync_app, name="sync")
master_app = typer.Typer(help="Security master (ticker -> security id over time)", no_args_is_help=True)
app.add_typer(master_app, name="master")
universe_app = typer.Typer(help="Point-in-time universes", no_args_is_help=True)
app.add_typer(universe_app, name="universe")
panel_app = typer.Typer(help="Daily security panel with split/dividend-aware returns", no_args_is_help=True)
app.add_typer(panel_app, name="panel")
features_app = typer.Typer(help="Feature groups (price, size, fund, short, news)", no_args_is_help=True)
app.add_typer(features_app, name="features")
console = Console()


def _setup_logging(verbose: bool) -> None:
    s = get_settings()
    s.ensure_dirs()
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [RichHandler(console=console, show_path=False, rich_tracebacks=True)]
    fh = logging.FileHandler(s.log_dir / f"ca-{dt.date.today():%Y-%m-%d}.log")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handlers.append(fh)
    logging.basicConfig(level=level, handlers=handlers, format="%(message)s", force=True)
    for noisy in ("botocore", "boto3", "urllib3", "httpx", "httpcore", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _provider():
    from collective_alpha.data.massive.provider import MassiveProvider

    return MassiveProvider()


def _run(command: str, fn, verbose: bool = False):
    _setup_logging(verbose)
    p = _provider()
    run_id = p.manifest.start_run(command)
    try:
        result = fn(p)
        p.manifest.finish_run(run_id, "ok", json.dumps(result, default=str))
        console.print_json(json.dumps(result, default=str))
        return result
    except Exception as e:
        p.manifest.finish_run(run_id, "error", str(e))
        raise
    finally:
        p.close()


DateOpt = Annotated[dt.datetime | None, typer.Option(formats=["%Y-%m-%d"])]


@app.command()
def probe(verbose: bool = False):
    """Report which REST endpoints and flat-file datasets this account can reach."""
    _setup_logging(verbose)
    p = _provider()
    res = p.probe()
    t = Table(title="REST endpoints")
    t.add_column("endpoint")
    t.add_column("ok")
    t.add_column("detail")
    for k, v in res["rest"].items():
        t.add_row(k, "[green]yes" if v["ok"] else "[red]no", str({kk: vv for kk, vv in v.items() if kk != "ok"}))
    console.print(t)
    console.print("[bold]Flat files[/bold]")
    console.print_json(json.dumps(res["flatfiles"], default=str))
    p.close()


@app.command()
def status(errors: bool = False):
    """Manifest summary: what is downloaded / converted per dataset."""
    from collective_alpha.storage.manifest import Manifest

    s = get_settings()
    if not s.manifest_path.exists():
        console.print("no manifest yet")
        return
    m = Manifest(s.manifest_path)
    t = Table(title=f"manifest @ {s.data_root}")
    for c in ("source", "dataset", "files", "downloaded", "converted", "errors", "first", "last", "raw GB", "rows"):
        t.add_column(c)
    for row in m.summary():
        t.add_row(*[str(x) for x in row])
    console.print(t)
    if errors:
        for k, e in m.errors():
            console.print(f"[red]{k}[/red]: {e}")
    import shutil

    du = shutil.disk_usage(s.data_root)
    console.print(f"disk free: {du.free / 1e9:.1f} GB of {du.total / 1e9:.1f} GB")


@app.command()
def coverage(dataset: str = typer.Argument("day_aggs_v1"), start: DateOpt = None, end: DateOpt = None):
    """List trading days with no curated file for a flat-file dataset."""
    _setup_logging(False)
    p = _provider()
    miss = p.missing_days(dataset, start.date() if start else None, end.date() if end else None)
    console.print(f"{dataset}: {len(miss)} missing trading days")
    for d in miss[:50]:
        console.print(f"  {d}")
    if len(miss) > 50:
        console.print(f"  … {len(miss) - 50} more")
    p.close()


@sync_app.command("reference")
def sync_reference(
    details: bool = typer.Option(False, help="Also fan out ticker details/events (slow)"), verbose: bool = False
):
    """Tickers (active + delisted), types, exchanges, conditions, holidays."""
    _run("sync reference", lambda p: p.sync_reference(details=details), verbose)


@sync_app.command("details")
def sync_details(verbose: bool = False):
    """Ticker details + ticker events for every active ticker."""
    _run("sync details", lambda p: p.sync_ticker_details(), verbose)


@sync_app.command("tickers-pit")
def sync_tickers_pit(verbose: bool = False):
    """Monthly point-in-time snapshots of active tickers (resolves symbol reuse)."""
    _run("sync tickers-pit", lambda p: p.sync_tickers_pit(), verbose)


@sync_app.command("sec-facts")
def sync_sec_facts(
    source: str = typer.Option("bulk", help="bulk (1.4 GB nightly archive, all CIKs at once) | api (per-CIK, 8 req/s)"),
    verbose: bool = False,
):
    """SEC EDGAR company facts (shares outstanding, basic fundamentals) for every CIK in `securities`."""
    from collective_alpha.data.sec.provider import SecProvider

    _setup_logging(verbose)
    p = SecProvider()
    run_id = p.manifest.start_run(f"sync sec-facts {source}")
    try:
        res = p.sync_facts(source=source)
        p.manifest.finish_run(run_id, "ok", json.dumps(res, default=str))
        console.print_json(json.dumps(res, default=str))
    except Exception as e:
        p.manifest.finish_run(run_id, "error", str(e))
        raise
    finally:
        p.close()


@sync_app.command("corporate-actions")
def sync_ca(verbose: bool = False):
    """Splits, dividends, IPOs."""
    _run("sync corporate-actions", lambda p: p.sync_corporate_actions(), verbose)


@sync_app.command("fundamentals")
def sync_fund(verbose: bool = False):
    """Financial statements, ratios, float (tier permitting)."""
    _run("sync fundamentals", lambda p: p.sync_fundamentals(), verbose)


@sync_app.command("monthly")
def sync_monthly(
    table: str = typer.Argument(..., help="news | short_interest | short_volume"),
    start: DateOpt = None,
    end: DateOpt = None,
    verbose: bool = False,
):
    """Time-partitioned REST tables."""
    _run(
        f"sync {table}",
        lambda p: p.sync_monthly(table, start.date() if start else None, end.date() if end else None),
        verbose,
    )


@sync_app.command("bars")
def sync_bars(
    dataset: str = typer.Argument("day_aggs_v1", help="day_aggs_v1 | minute_aggs_v1 | trades_v1 | quotes_v1"),
    start: DateOpt = None,
    end: DateOpt = None,
    convert: bool | None = typer.Option(None, help="Convert to parquet (default: yes for aggs, no for ticks)"),
    verbose: bool = False,
):
    """Download a flat-file dataset for a date range and convert to Parquet."""
    _run(
        f"sync bars {dataset}",
        lambda p: p.sync_bars(dataset, start.date() if start else None, end.date() if end else None, convert),
        verbose,
    )


@master_app.command("build")
def master_build(gap_days: int = 30, verbose: bool = False):
    """Build security_master + securities tables from bars, PIT ticker snapshots and dated lookups."""
    from collective_alpha.universe.builder import build_security_master

    _run("master build", lambda p: build_security_master(p, gap_days=gap_days), verbose)


@master_app.command("lookup")
def master_lookup(ticker: str):
    """Show every security a ticker has referred to."""
    from collective_alpha.universe.builder import load_master

    m = load_master().filter(pl.col("ticker") == ticker.upper()).sort("valid_from")
    console.print(
        m.select("ticker", "security_id", "valid_from", "valid_to", "name", "type", "method")
        .to_pandas()
        .to_string(index=False)
    )


@universe_app.command("build")
def universe_build(
    name: str = typer.Argument("all", help="universe name from config/universes.toml, or 'all'"), verbose: bool = False
):
    """Build daily membership tables (curated/universes/name=<name>/year=YYYY)."""
    from collective_alpha.universe.attributes import build_security_attributes
    from collective_alpha.universe.builder import _all_snapshots, load_master, load_securities
    from collective_alpha.universe.universes import build_and_write, load_specs

    def run(p):
        s = p.s
        master, securities = load_master(s), load_securities(s)
        attrs = build_security_attributes(s, master, _all_snapshots(s, "tickers_pit"))
        specs = load_specs()
        names = list(specs) if name == "all" else [name]
        return {n: build_and_write(s, specs[n], master, attrs, securities) for n in names}

    _run(f"universe build {name}", run, verbose)


@universe_app.command("stats")
def universe_stats_cmd(name: str):
    """Members and turnover per rebalance."""
    from collective_alpha.universe.universes import load_universe, universe_stats

    st = universe_stats(load_universe(get_settings(), name))
    console.print(st.to_pandas().to_string(index=False))


@panel_app.command("build")
def panel_build(verbose: bool = False):
    """Build curated/panel/year=YYYY from bars, security master, splits and dividends."""
    from collective_alpha.panel.build import build_and_write

    _run("panel build", lambda p: build_and_write(p.s), verbose)


@panel_app.command("check")
def panel_check(threshold: float = 1.0):
    """Sanity report and the largest absolute returns."""
    from collective_alpha.panel.build import check_panel, extreme_returns, load_panel

    panel = load_panel()
    console.print_json(json.dumps(check_panel(panel), default=str))
    console.print(extreme_returns(panel, threshold).to_pandas().to_string(index=False))


@features_app.command("build")
def features_build(
    group: str = typer.Argument("all", help="price | size | fund | short | news | all"), verbose: bool = False
):
    """Materialise feature groups under curated/features/<group>/year=YYYY."""
    from collective_alpha.features.base import build_groups

    groups = None if group == "all" else [group]
    _run(f"features build {group}", lambda p: build_groups(groups, p.s), verbose)


@features_app.command("list")
def features_list():
    """Built feature groups and their columns."""
    from collective_alpha.features.base import list_features

    console.print_json(json.dumps(list_features()))


@app.command()
def convert(dataset: str = typer.Argument("day_aggs_v1"), workers: int | None = None, verbose: bool = False):
    """Convert already-downloaded raw files that have no curated parquet yet."""
    _run(f"convert {dataset}", lambda p: {"converted": p.convert_pending(dataset, workers)}, verbose)


@app.command()
def backfill(details: bool = True, verbose: bool = False):
    """Everything, back to the entitlement horizon."""
    _run("backfill", lambda p: p.backfill(details=details), verbose)


@app.command()
def update(days_back: int = 5, details: bool = False, verbose: bool = False):
    """Incremental daily refresh."""
    _run("update", lambda p: p.update(days_back=days_back, details=details), verbose)


@app.command()
def config():
    """Print effective settings."""
    s = get_settings()
    console.print_json(s.model_dump_json())
    console.print(
        f"api key file exists: {Path(s.api_key_file).exists()}  s3 key file exists: {Path(s.s3_key_file).exists()}"
    )


if __name__ == "__main__":
    app()
