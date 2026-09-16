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
intraday_app = typer.Typer(help="Intraday aggregates from minute bars", no_args_is_help=True)
app.add_typer(intraday_app, name="intraday")
eval_app = typer.Typer(help="Signal evaluation", no_args_is_help=True)
app.add_typer(eval_app, name="eval")
bt_app = typer.Typer(help="Backtests", no_args_is_help=True)
app.add_typer(bt_app, name="backtest")
pf_app = typer.Typer(help="Portfolio construction with a risk model", no_args_is_help=True)
app.add_typer(pf_app, name="portfolio")
trade_app = typer.Typer(help="Paper/live trading pipeline", no_args_is_help=True)
app.add_typer(trade_app, name="trade")
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


@eval_app.command("forward")
def eval_forward(delist_return: float = 0.0, verbose: bool = False):
    """Build curated/forward_returns (close-to-close over several horizons, next-day open/close)."""
    from collective_alpha.eval.report import build_forward_returns

    _run("eval forward", lambda p: build_forward_returns(p.s, delist_return), verbose)


@eval_app.command("feature")
def eval_feature(
    feature: str,
    universe: str = "liquid_1500",
    horizon: int = 5,
    sign: float = 1.0,
    quantiles: int = 10,
    start: DateOpt = None,
    end: DateOpt = None,
    neutralize_by: Annotated[
        str | None, typer.Option(help="attribute column to demean within, e.g. primary_exchange")
    ] = None,
    as_json: bool = False,
):
    """Evaluate a raw feature as a signal (cross-sectional rank of sign * feature)."""
    from collective_alpha.eval.report import evaluate_feature

    _setup_logging(False)
    rep = evaluate_feature(
        feature,
        universe,
        horizon,
        sign,
        quantiles,
        start.date() if start else None,
        end.date() if end else None,
        neutralize_by,
    )
    if as_json:
        console.print_json(json.dumps(rep.to_dict(), default=str))
    else:
        console.print(rep.to_markdown())


@bt_app.command("feature")
def backtest_feature_cmd(
    feature: str,
    universe: str = "liquid_1500",
    sign: float = 1.0,
    scheme: str = typer.Option("ls_quantile", help="ls_quantile | long_top | signal_weighted"),
    rebalance: str = typer.Option("21", help="sessions between rebalances, or monthly/weekly"),
    quantiles: int = 10,
    gross: float = 2.0,
    max_weight: float = 0.05,
    delay: int = 1,
    commission_bps: float = 0.5,
    half_spread_bps: float = 3.0,
    slippage_bps: float = 2.0,
    borrow_rate: float = 0.005,
    impact: bool = typer.Option(False, help="square-root impact on ADV participation (needs price features)"),
    start: DateOpt = None,
    end: DateOpt = None,
    as_json: bool = False,
):
    """Backtest a feature as a signal with a simple portfolio scheme and cost model."""
    from collective_alpha.backtest.engine import CostModel
    from collective_alpha.backtest.run import backtest_feature

    _setup_logging(False)
    rb: int | str = int(rebalance) if rebalance.isdigit() else rebalance
    res = backtest_feature(
        feature,
        universe,
        sign,
        scheme,
        rb,
        quantiles,
        gross,
        max_weight,
        CostModel(commission_bps, half_spread_bps, slippage_bps, borrow_rate, 0.1 if impact else 0.0),
        delay,
        start.date() if start else None,
        end.date() if end else None,
        impact,
    )
    if as_json:
        console.print_json(json.dumps({"summary": res.summary(), "by_year": res.by_year().to_dicts()}, default=str))
    else:
        console.print(res.to_markdown())


@pf_app.command("feature")
def portfolio_feature_cmd(
    feature: str,
    universe: str = "liquid_1500",
    sign: float = 1.0,
    method: str = typer.Option("heuristic", help="heuristic | mvo"),
    rebalance: str = "21",
    gross: float = 2.0,
    net: float = 0.0,
    max_weight: float = 0.03,
    sector_neutral: bool = True,
    vol_target: float | None = None,
    risk_aversion: float = 5.0,
    turnover_penalty: float = 0.0,
    max_adv_participation: float | None = None,
    delay: int = 1,
    start: DateOpt = None,
    end: DateOpt = None,
    as_json: bool = False,
):
    """Backtest a feature signal through portfolio construction (sector neutral, capped, vol-targeted)."""
    from collective_alpha.backtest.engine import BacktestConfig
    from collective_alpha.portfolio.construct import PortfolioConfig
    from collective_alpha.portfolio.run import exposure_report, portfolio_feature

    _setup_logging(False)
    rb: int | str = int(rebalance) if rebalance.isdigit() else rebalance
    cfg = PortfolioConfig(
        method=method,
        gross=gross,
        net=net,
        max_weight=max_weight,
        sector_neutral=sector_neutral,
        vol_target=vol_target,
        risk_aversion=risk_aversion,
        turnover_penalty=turnover_penalty,
        max_adv_participation=max_adv_participation,
    )
    res, diag = portfolio_feature(
        feature,
        universe,
        sign,
        cfg,
        rb,
        BacktestConfig(delay=delay),
        start.date() if start else None,
        end.date() if end else None,
    )
    if as_json:
        console.print_json(
            json.dumps(
                {"summary": res.summary(), "by_year": res.by_year().to_dicts(), "exposures": exposure_report(diag)},
                default=str,
            )
        )
    else:
        console.print(res.to_markdown())
        console.print_json(json.dumps(exposure_report(diag), default=str))


@trade_app.command("run")
def trade_run(strategy: str, date: DateOpt = None, force_rebalance: bool = False, verbose: bool = False):
    """Run one session: fill pending orders at the open, mark at the close, rebalance if due."""
    from collective_alpha.execution.pipeline import run_session
    from collective_alpha.execution.strategy import StrategyConfig

    _setup_logging(verbose)
    cfg = StrategyConfig.load(strategy)
    console.print_json(
        json.dumps(run_session(cfg, date.date() if date else None, force_rebalance=force_rebalance), default=str)
    )


@trade_app.command("replay")
def trade_replay(strategy: str, start: DateOpt = None, end: DateOpt = None, verbose: bool = False):
    """Replay the pipeline over past sessions on the paper broker."""
    from collective_alpha.execution.pipeline import nav_frame, replay
    from collective_alpha.execution.strategy import StrategyConfig

    _setup_logging(verbose)
    cfg = StrategyConfig.load(strategy)
    res = replay(cfg, start.date(), end.date())
    errs = [r for r in res if "error" in r]
    console.print(f"{len(res)} sessions, {len(errs)} errors, {sum(1 for r in res if r.get('rebalance'))} rebalances")
    console.print(nav_frame(cfg).tail(5).to_pandas().to_string(index=False))


@trade_app.command("status")
def trade_status(strategy: str):
    """Paper book status."""
    from collective_alpha.execution.pipeline import status
    from collective_alpha.execution.strategy import StrategyConfig

    console.print_json(json.dumps(status(StrategyConfig.load(strategy)), default=str))


@trade_app.command("export")
def trade_export(strategy: str, path: Path = Path("orders.csv")):
    """Export pending orders to CSV for manual execution at a broker."""
    from collective_alpha.execution.pipeline import export_orders
    from collective_alpha.execution.strategy import StrategyConfig

    n = export_orders(StrategyConfig.load(strategy), path)
    console.print(f"{n} pending orders written to {path}")


@intraday_app.command("build")
def intraday_build(
    start: DateOpt = None, end: DateOpt = None, force: bool = False, workers: int | None = None, verbose: bool = False
):
    """Aggregate minute bars into one row per security and session (incremental)."""
    from collective_alpha.intraday.build import build

    _run(
        "intraday build",
        lambda p: build(p.s, start.date() if start else None, end.date() if end else None, force, workers),
        verbose,
    )


@intraday_app.command("spreads")
def intraday_spreads(universe: str = "all_common", start: DateOpt = None, end: DateOpt = None):
    """Validate estimated effective spreads: they must fall as dollar volume and price rise."""
    from collective_alpha.intraday.spread import spread_report

    _setup_logging(False)
    by_decile, named = spread_report(
        get_settings(), universe, start.date() if start else None, end.date() if end else None
    )
    if by_decile.height == 0:
        console.print("no intraday spreads yet: run `ca intraday build`")
        return
    console.print(f"[bold]by dollar-volume decile[/bold] ({universe})")
    console.print(by_decile.to_pandas().to_string(index=False))
    console.print("[bold]named examples[/bold] (tick_bp is one cent as a fraction of price)")
    console.print(named.to_pandas().to_string(index=False))


@intraday_app.command("check")
def intraday_check():
    """Session coverage of the intraday table."""
    from collective_alpha.intraday.build import coverage

    c = coverage()
    console.print("no intraday table yet" if c.height == 0 else c.to_pandas().to_string(index=False))


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
