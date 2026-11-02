"""`subspace` command line: start data services and inspect the store."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Annotated

import typer

from . import _services
from ._config import ConfigError, Settings, load_settings
from ._datasets.flat import FLAT_DATASETS
from ._datasets.rest import REST_DATASETS
from ._dates import today_et
from ._layout import Layout

app = typer.Typer(help="massive-subspace-stream: Massive market data for the Collective.", no_args_is_help=True,
                  pretty_exceptions_show_locals=False)
backfill_app = typer.Typer(help="Download historical data.", no_args_is_help=True)
stream_app = typer.Typer(help="Live websocket stream service.", no_args_is_help=True)
service_app = typer.Typer(help="Install background services (macOS launchd).", no_args_is_help=True)
query_app = typer.Typer(help="Quick look at stored data.", no_args_is_help=True)
identity_app = typer.Typer(help="Security master: which company each ticker meant on each day.",
                           no_args_is_help=True)
app.add_typer(identity_app, name="identity")
app.add_typer(backfill_app, name="backfill")
app.add_typer(stream_app, name="stream")
app.add_typer(service_app, name="service")
app.add_typer(query_app, name="query")

log = logging.getLogger("massive_subspace_stream")
_state: dict = {}


def _settings() -> Settings:
    if "settings" not in _state:
        overrides = {"data_root": _state["data_root"]} if _state.get("data_root") else {}
        _state["settings"] = load_settings(**overrides)
    return _state["settings"]


def _setup_logging(verbose: bool) -> None:
    root = logging.getLogger("massive_subspace_stream")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if root.handlers:
        return
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    root.addHandler(console)
    try:
        layout = Layout(_settings().data_root)
        layout.logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(layout.logs_dir / "subspace.log")
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s"))
        root.addHandler(file_handler)
    except OSError:
        pass


@app.callback()
def main(
    data_root: Annotated[Path | None, typer.Option(help="Override the data root (default ~/market_data/massive).")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    _state["data_root"] = data_root
    _setup_logging(verbose)


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _fmt_bytes(n: float | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:,.1f} TB"


def _report(results) -> None:
    failed = False
    for r in results:
        typer.echo("  " + r.line())
        failed = failed or not r.ok
    if failed:
        typer.secho("Some items failed; re-run the same command to retry them.", fg=typer.colors.YELLOW)
        raise typer.Exit(1)


def _ingestor():
    from ._ingest.backfill import Ingestor

    return Ingestor(_settings())


def _split(value: str | None) -> list[str] | None:
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


# ---------------------------------------------------------------------- init / status
@app.command()
def init() -> None:
    """Create the data folders, check credentials, and report what your plan includes."""
    from ._ingest.backfill import disk_report
    from ._sources.flatfiles import FlatFileClient
    from ._sources.rest import NotEntitledError, RestClient, RestError

    s = _settings()
    layout = Layout(s.data_root)
    layout.ensure()
    typer.echo(f"Data root:   {layout.root}")
    typer.echo(f"Config file: {s.config_file or '(none; using defaults)'}")
    typer.echo(f"Env file:    {s.env_file if s.env_file and s.env_file.exists() else '(not found)'}")
    disk = disk_report(layout)
    typer.echo(f"Disk free:   {_fmt_bytes(disk['free_bytes'])}")
    ok = True

    try:
        with RestClient(s.require_api_key(), s.rest_url) as rest:
            rest.get("/v1/marketstatus/now")
            typer.secho("REST API:    OK", fg=typer.colors.GREEN)
            checks = {
                "trades/quotes": "/v3/trades/AAPL?limit=1",
                "financials": "/stocks/financials/v1/ratios?ticker=AAPL&limit=1",
                "short interest": "/stocks/v1/short-interest?ticker=AAPL&limit=1",
            }
            for label, path in checks.items():
                try:
                    rest.get(path)
                    typer.echo(f"  {label:<15} included")
                except NotEntitledError:
                    typer.secho(f"  {label:<15} NOT in your plan", fg=typer.colors.YELLOW)
    except (ConfigError, RestError) as exc:
        ok = False
        typer.secho(f"REST API:    {exc}", fg=typer.colors.RED)

    try:
        key_id, secret = s.require_s3()
        flat = FlatFileClient(s.s3_endpoint, s.s3_bucket, key_id, secret)
        recent = flat.list_files("day_aggs_v1", today_et() - timedelta(days=10), today_et())
        if recent:
            typer.secho(f"Flat files:  OK (latest day file: {recent[-1].day})", fg=typer.colors.GREEN)
        else:
            typer.secho("Flat files:  connected, but no files in the last 10 days", fg=typer.colors.YELLOW)
    except Exception as exc:
        ok = False
        typer.secho(f"Flat files:  {type(exc).__name__}: {exc}", fg=typer.colors.RED)
    if not ok:
        raise typer.Exit(1)
    typer.echo("\nNext: `subspace backfill initial` (day + minute bars from 2003, about 100 GB).")


@app.command()
def status() -> None:
    """What is stored, how big it is, and whether the stream service is running."""
    from ._catalog import Catalog
    from ._ingest.backfill import disk_report
    from ._ingest.stream import read_status

    s = _settings()
    layout = Layout(s.data_root)
    layout.ensure()
    catalog = Catalog(layout.catalog_path)
    rows = catalog.summary()
    typer.echo(f"Data root: {layout.root}\n")
    if not rows:
        typer.echo("Nothing stored yet. Start with `subspace init`, then `subspace backfill initial`.")
    else:
        typer.echo(f"{'dataset':<22}{'parts':>7}  {'first':<11} {'last':<11}{'rows':>16}{'size':>12}")
        for r in rows:
            typer.echo(f"{r['dataset']:<22}{r['partitions']:>7}  {str(r['first'])[:10]:<11} {str(r['last'])[:10]:<11}"
                       f"{r['rows'] or 0:>16,}{_fmt_bytes(r['bytes']):>12}")
    disk = disk_report(layout)
    typer.echo(f"\nOn disk: {_fmt_bytes(disk['data_bytes'])}   Free: {_fmt_bytes(disk['free_bytes'])}")
    build = catalog.get_state("identity", "last_build")
    if build:
        import json

        b = json.loads(build)
        typer.echo(f"Security master: {b['securities']:,} securities, built {b['built_at']} "
                   f"(bars through {b['data_end']})")
    else:
        typer.echo("Security master: not built (run `subspace identity build`)")
    info = read_status(layout)
    if info["running"]:
        last = info.get("last_status", {})
        typer.secho(f"Stream: running (pid {info['pid']}), last message {last.get('last_message_at') or '-'}",
                    fg=typer.colors.GREEN)
    else:
        reason = info.get("last_status", {}).get("stopped_reason")
        typer.echo(f"Stream: not running{f' (stopped: {reason})' if reason else ''}")
    typer.echo(f"Massive plan: {s.plan} (stream: {s.stream_url}"
               + (f", history from {s.history_start()})" if s.history_start() else ")"))


@app.command()
def datasets() -> None:
    """List every dataset this package can download."""
    from ._store import Store

    for name, desc in Store.datasets().items():
        typer.echo(f"  {name:<22} {desc}")


# ---------------------------------------------------------------------- backfill
@backfill_app.command("initial")
def backfill_initial(
    since: Annotated[str | None, typer.Option(help="First date (default: earliest available, 2003-09-10).")] = None,
    minute: Annotated[bool, typer.Option(help="Include minute bars (about 100 GB for full history).")] = True,
    rest: Annotated[bool, typer.Option(help="Include REST datasets (reference, fundamentals, news, ...).")] = True,
    workers: Annotated[int | None, typer.Option(help="Parallel downloads.")] = None,
) -> None:
    """First-time load: reference data, day bars, minute bars, then fundamentals, short data and news.
    Safe to stop and re-run; finished days are skipped."""
    _report(_ingestor().initial(since=since, minute=minute, rest=rest, workers=workers))


@backfill_app.command("history")
def backfill_history(
    dataset: Annotated[str, typer.Option(help=f"One of: {', '.join(FLAT_DATASETS)}.")],
    start: Annotated[str, typer.Option(help="First date, YYYY-MM-DD.")],
    end: Annotated[str | None, typer.Option(help="Last date (default: today).")] = None,
    symbols: Annotated[str | None, typer.Option(help="Comma-separated tickers (trades/quotes only).")] = None,
    workers: Annotated[int | None, typer.Option(help="Parallel downloads.")] = None,
    force: Annotated[bool, typer.Option(help="Re-download days that are already stored.")] = False,
) -> None:
    """Fill a date range of a flat-file dataset (bars, or on-demand trades/quotes)."""
    if dataset not in FLAT_DATASETS:
        _fail(f"unknown dataset '{dataset}'; choose from {', '.join(FLAT_DATASETS)}")
    if dataset in ("trades", "quotes") and not symbols:
        typer.secho(f"No --symbols given: storing every ticker's {dataset} (several GB per day).",
                    fg=typer.colors.YELLOW)
    _report([_ingestor().flat(dataset, start, end, tickers=_split(symbols), workers=workers, force=force)])


@backfill_app.command("rest")
def backfill_rest(
    names: Annotated[list[str] | None, typer.Argument(help=f"Datasets (default all): {', '.join(REST_DATASETS)}.")] = None,
    full: Annotated[bool, typer.Option(help="Ignore saved progress and re-pull full history.")] = False,
) -> None:
    """Refresh REST datasets (incremental by default)."""
    _report(_ingestor().rest(names or None, full=full))


@app.command()
def sync() -> None:
    """Daily catch-up: new day/minute bars plus incremental REST refreshes; prunes old live data."""
    _report(_ingestor().sync())


@app.command()
def verify(
    dataset: Annotated[str, typer.Option(help=f"One of: {', '.join(FLAT_DATASETS)}.")] = "bars_minute",
    start: Annotated[str | None, typer.Option()] = None,
    end: Annotated[str | None, typer.Option()] = None,
    repair: Annotated[bool, typer.Option(help="Re-download missing or broken days.")] = False,
) -> None:
    """Find days that are missing from the store or whose files are damaged."""
    report = _ingestor().verify(dataset, start, end, repair=repair)
    for kind in ("missing", "broken", "unrepairable"):
        days = report[kind]
        if kind == "unrepairable" and not days:
            continue
        preview = ", ".join(days[:10]) + (" ..." if len(days) > 10 else "")
        typer.echo(f"{kind}: {len(days)} day(s){': ' + preview if days else ''}")
    if report["unrepairable"]:
        typer.echo(f"Unrepairable days are outside the {_settings().plan} plan; restore them from a backup.")
    if set(report["missing"] + report["broken"]) - set(report["unrepairable"]) and not repair:
        typer.echo("Run again with --repair to fix them.")


# ---------------------------------------------------------------------- stream
@stream_app.command("run")
def stream_run(
    channels: Annotated[str | None, typer.Option(help="Channels for ALL tickers, e.g. AM,A (default from settings).")] = None,
    symbols: Annotated[str | None, typer.Option(help="Watchlist tickers for --watch-channels.")] = None,
    watch_channels: Annotated[str | None, typer.Option(help="Channels for the watchlist only, e.g. T,Q.")] = None,
    flush_seconds: Annotated[float | None, typer.Option(help="How often buffered data is written.")] = None,
) -> None:
    """Run the stream in the foreground (Ctrl-C to stop). Used by the launchd service."""
    from ._ingest.stream import StreamRefused, StreamService

    try:
        service = StreamService(_settings(), _split(channels), _split(symbols), _split(watch_channels), flush_seconds)
        asyncio.run(service.run())
    except (StreamRefused, ConfigError, ValueError) as exc:
        # A restart can't fix these. Under launchd a clean exit stops the service instead of
        # restarting it every 30 seconds (KeepAlive only on unsuccessful exits).
        typer.secho(f"{exc}\nThe stream service stopped; fix the key, plan or channels and start it again.",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(0 if os.environ.get(_services.SERVICE_ENV) else 1) from None
    except (RuntimeError, PermissionError) as exc:
        _fail(str(exc))


@stream_app.command("start")
def stream_start(
    channels: Annotated[str | None, typer.Option()] = None,
    symbols: Annotated[str | None, typer.Option()] = None,
    watch_channels: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Start the stream in the background."""
    from ._ingest.stream import read_status

    s = _settings()
    layout = Layout(s.data_root)
    layout.ensure()
    if read_status(layout)["running"]:
        _fail("stream is already running (`subspace stream status`)")
    args = [_services.subspace_executable(), "--data-root", str(layout.root), "stream", "run"]
    for flag, value in (("--channels", channels), ("--symbols", symbols), ("--watch-channels", watch_channels)):
        if value:
            args += [flag, value]
    log_path = layout.logs_dir / "stream.log"
    with open(log_path, "a") as out:
        subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    for _ in range(20):
        time.sleep(0.5)
        info = read_status(layout)
        if info["running"]:
            typer.secho(f"stream started (pid {info['pid']}); log: {log_path}", fg=typer.colors.GREEN)
            return
    _fail(f"stream did not start; see {log_path}")


@stream_app.command("stop")
def stream_stop() -> None:
    """Stop the background stream (flushes buffered data first)."""
    from ._ingest.stream import read_status

    layout = Layout(_settings().data_root)
    info = read_status(layout)
    if not info["running"]:
        typer.echo("stream is not running")
        return
    if _services.status("stream").get("loaded"):
        typer.secho("Note: the launchd stream service will restart it; use `subspace service remove --only stream`.",
                    fg=typer.colors.YELLOW)
    os.kill(info["pid"], signal.SIGTERM)
    for _ in range(60):
        time.sleep(0.5)
        if not read_status(layout)["running"]:
            typer.echo("stream stopped")
            return
    _fail(f"stream (pid {info['pid']}) did not stop within 30s")


@stream_app.command("status")
def stream_status() -> None:
    """Show whether the stream is running and what it has received."""
    import json

    from ._ingest.stream import read_status

    typer.echo(json.dumps(read_status(Layout(_settings().data_root)), indent=2))


# ---------------------------------------------------------------------- services
def _service_names(only: str | None) -> list[str]:
    if only and only not in _services.LABELS:
        _fail(f"--only must be one of {', '.join(_services.LABELS)}")
    return [only] if only else list(_services.LABELS)


@service_app.command("install")
def service_install(only: Annotated[str | None, typer.Option(help="sync or stream (default both).")] = None) -> None:
    """Install launchd agents: daily sync (Tue-Sat morning) and the always-on stream."""
    s = _settings()
    for name in _service_names(only):
        path = _services.install(name, s)
        typer.secho(f"installed {name}: {path}", fg=typer.colors.GREEN)


@service_app.command("remove")
def service_remove(only: Annotated[str | None, typer.Option(help="sync or stream (default both).")] = None) -> None:
    """Unload and delete the launchd agents."""
    for name in _service_names(only):
        typer.echo(f"{name}: {'removed' if _services.remove(name) else 'was not installed'}")


@service_app.command("status")
def service_status() -> None:
    """Show launchd service state."""
    for name in _services.LABELS:
        typer.echo(f"{name}: {_services.status(name)}")


# ---------------------------------------------------------------------- identity
@identity_app.command("build")
def identity_build(
    fetch: Annotated[bool, typer.Option(help="Download missing snapshots and dated lookups first.")] = True,
) -> None:
    """Build or refresh the security master (first run downloads ~210 snapshots and tens of
    thousands of dated lookups, 10-20 minutes; later runs only fetch what is new)."""
    _report([_ingestor().identity(fetch=fetch)])


@identity_app.command("check")
def identity_check() -> None:
    """Coverage, consistency and regression checks on the built security master."""
    import json

    import polars as pl

    from ._identity.checks import run_checks
    from ._store import Store

    store = Store(settings=_settings())
    try:
        master, secs = store.security_master(), store.securities()
    except FileNotFoundError as exc:
        _fail(str(exc))
    layout = store.layout
    bars = store.scan("bars_day").select("ticker", "date").collect()
    report = run_checks(master, secs, bars, pl.read_parquet(layout.identity_file("splits")),
                        pl.read_parquet(layout.identity_file("dividends")))
    cases = report.pop("regression")
    typer.echo(json.dumps(report, indent=2, default=str))
    failed = 0
    for c in cases:
        color = {"pass": typer.colors.GREEN, "FAIL": typer.colors.RED}.get(c["result"], typer.colors.YELLOW)
        typer.secho(f"  {c['result']:<7} {c['case']}  ({c.get('detail', '')})", fg=color)
        failed += c["result"] == "FAIL"
    if failed:
        raise typer.Exit(1)


@identity_app.command("show")
def identity_show(ticker: Annotated[str, typer.Argument(help="A ticker or security id.")]) -> None:
    """Every security a ticker has meant, and every ticker those securities used."""
    import polars as pl

    from ._store import Store

    store = Store(settings=_settings())
    try:
        master = store.security_master()
    except FileNotFoundError as exc:
        _fail(str(exc))
    ticker = ticker.strip().upper()
    ids = master.filter((pl.col("ticker") == ticker) | (pl.col("security_id") == ticker))["security_id"].unique()
    if not ids.len():
        _fail(f"{ticker}: not in the security master")
    rows = master.filter(pl.col("security_id").is_in(ids.to_list())).sort("security_id", "valid_from").select(
        "security_id", "ticker", "valid_from", "valid_to", "name", "type", "cik", "method", "linked_by")
    _show(rows, None, 50)


# ---------------------------------------------------------------------- query
def _show(df, csv: Path | None, rows: int) -> None:
    import polars as pl

    if csv:
        df.write_csv(csv)
        typer.echo(f"wrote {df.height:,} rows to {csv}")
        return
    with pl.Config(tbl_rows=rows, tbl_cols=20, tbl_width_chars=200):
        typer.echo(df)


@query_app.command("bars")
def query_bars(
    tickers: Annotated[str, typer.Argument(help="Comma-separated tickers.")],
    start: Annotated[str | None, typer.Option()] = None,
    end: Annotated[str | None, typer.Option()] = None,
    freq: Annotated[str, typer.Option(help="1d or 1min.")] = "1d",
    adjust: Annotated[str | None, typer.Option(help="splits or all.")] = None,
    session: Annotated[str, typer.Option(help="all, regular, or extended (minute bars).")] = "all",
    by: Annotated[str, typer.Option(help="ticker, or security (whole history across renames).")] = "ticker",
    csv: Annotated[Path | None, typer.Option(help="Write to CSV instead of printing.")] = None,
    rows: Annotated[int, typer.Option(help="Rows to print.")] = 20,
) -> None:
    """Print (or export) bars for some tickers."""
    from ._store import Store

    store = Store(settings=_settings())
    try:
        df = store.bars(tickers, start, end, freq=freq, adjust=adjust, session=session, by=by)
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
    _show(df, csv, rows)


@query_app.command("dataset")
def query_dataset(
    name: Annotated[str, typer.Argument(help=f"One of: {', '.join(REST_DATASETS)}.")],
    symbols: Annotated[str | None, typer.Option()] = None,
    start: Annotated[str | None, typer.Option()] = None,
    end: Annotated[str | None, typer.Option()] = None,
    csv: Annotated[Path | None, typer.Option()] = None,
    rows: Annotated[int, typer.Option()] = 20,
) -> None:
    """Print (or export) a REST dataset, optionally filtered."""
    from ._store import Store

    try:
        df = Store(settings=_settings()).dataset(name, symbols, start, end)
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
    _show(df, csv, rows)


if __name__ == "__main__":
    app()
