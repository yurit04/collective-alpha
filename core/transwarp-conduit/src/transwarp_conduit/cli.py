"""`conduit` command line: look at the IB account, plan order sheets, send them, check fills."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Annotated

import polars as pl
import typer

app = typer.Typer(help="transwarp-conduit: execution through Interactive Brokers (paper first).",
                  no_args_is_help=True, pretty_exceptions_show_locals=False)
SHOW = ["ticker", "action", "trade_shares", "price", "trade_value", "post_weight", "est_cost_bps", "status", "note"]


@app.callback()
def main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
         port: Annotated[int | None, typer.Option(help="IB Gateway/TWS port (paper 4002/7497, live 4001/7496).")] = None
         ) -> None:
    root = logging.getLogger("transwarp_conduit")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not root.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        root.addHandler(h)
    app.info = {"port": port}


def _conduit():
    from ._conduit import Conduit
    from ._config import load_settings

    over = {"port": app.info["port"]} if getattr(app, "info", {}).get("port") else {}
    return Conduit(load_settings(**over))


def _run(fn):
    from ._config import ConduitError

    try:
        return fn()
    except ConduitError as exc:
        typer.secho(f"refused: {exc}", fg="red", err=True)
        raise typer.Exit(2) from None


def _targets(path: Path, on: str | None) -> pl.DataFrame:
    t = pl.read_parquet(path) if path.suffix == ".parquet" else pl.read_csv(path)
    missing = {"security_id", "weight"} - set(t.columns)
    if missing:
        raise typer.BadParameter(f"{path} needs columns security_id and weight (missing {', '.join(missing)})")
    if "date" in t.columns:
        t = t.with_columns(pl.col("date").cast(pl.Date))
        d = pl.lit(on).str.to_date() if on else pl.col("date").max()
        t = t.filter(pl.col("date") == d)
    return t


def _print_sheet(sheet, rows: int = 25) -> None:
    typer.echo(json.dumps(sheet.summary(), indent=1, default=str))
    with pl.Config(tbl_rows=rows, tbl_width_chars=160, tbl_cols=12, float_precision=4):
        typer.echo(sheet.orders.filter(pl.col("status") != "none").select(SHOW).head(rows))


@app.command()
def status() -> None:
    """Account (masked id), paper or live, NAV, positions, open orders from this package."""
    typer.echo(json.dumps(_run(lambda: _conduit().status()), indent=1, default=str))


@app.command()
def contracts(targets: Path, on: Annotated[str | None, typer.Option(help="Rebalance date in the file.")] = None,
              refresh: bool = False) -> None:
    """Map the targets' securities to IB contracts and show the ones that cannot be traded."""
    c = _conduit()
    t = _targets(targets, on)

    def go():
        with c.session() as b:
            return c.book.resolve(t["security_id"].to_list(), c.today(), b, refresh=refresh)

    res = _run(go)
    typer.echo(res.group_by("status").len().sort("len", descending=True))
    with pl.Config(tbl_rows=50, tbl_width_chars=200):
        typer.echo(res.filter(~pl.col("status").is_in(["ok", "override"]))
                   .select("security_id", "ticker", "status", "note"))


@app.command()
def plan(targets: Annotated[Path, typer.Argument(help="Parquet or CSV: security_id, weight [, date].")],
         on: Annotated[str | None, typer.Option(help="Rebalance date in the file (default: the latest).")] = None,
         capital: Annotated[float | None, typer.Option(help="Dollars to run (default: settings, else NAV).")] = None,
         drop_blocked: Annotated[bool, typer.Option(help="Drop blocked orders instead of blocking the sheet.")] = False,
         borrow_check: bool = True, refresh_contracts: bool = False) -> None:
    """Dry run: build and save an order sheet. Nothing is sent."""
    c = _conduit()
    t = _targets(targets, on)
    sheet = _run(lambda: c.plan(t, capital=capital, drop_blocked=drop_blocked, check_borrow=borrow_check,
                                refresh_contracts=refresh_contracts))
    _print_sheet(sheet)
    typer.echo(f"saved: {c.journal.sheets / sheet.sheet_id}")


@app.command()
def show(sheet_id: str, rows: int = 50) -> None:
    """An order sheet's summary and orders."""
    _print_sheet(_conduit().sheet(sheet_id), rows)


@app.command()
def submit(sheet_id: str, live: Annotated[bool, typer.Option(help="Allow a live (non-paper) account.")] = False,
           yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation.")] = False) -> None:
    """Send a planned sheet's orders (market-on-close by default) after every check passes."""
    c = _conduit()
    sheet = c.sheet(sheet_id)
    _print_sheet(sheet, 10)
    if not yes:
        kind = "LIVE" if live else "paper"
        typer.confirm(f"Send {sheet.to_send.height} {kind} {sheet.order_type} orders?", abort=True)
    placed = _run(lambda: c.submit(sheet_id, live=live))
    typer.echo(placed.group_by("status").len())


@app.command()
def orders(sheet_id: Annotated[str | None, typer.Argument()] = None) -> None:
    """Orders from this package (open and today's completed), optionally for one sheet."""
    with pl.Config(tbl_rows=60, tbl_width_chars=200):
        typer.echo(_run(lambda: _conduit().orders(sheet_id)))


@app.command()
def fills() -> None:
    """Pull today's executions into the journal and show the latest."""
    with pl.Config(tbl_rows=40, tbl_width_chars=200):
        typer.echo(_run(lambda: _conduit().sync_fills()).tail(40))


@app.command()
def reconcile(sheet_id: str) -> None:
    """Expected against actual positions for a sheet, and fill prices against the decision price."""
    r = _run(lambda: _conduit().reconcile(sheet_id))
    bad = r.filter((pl.col("diff") != 0) | (pl.col("unfilled") != 0))
    typer.echo(f"{r.height} names, {int((r['diff'] != 0).sum())} position mismatches, "
               f"{int((r['unfilled'] != 0).sum())} not fully filled, "
               f"median slippage {r['slippage_bps'].median() or 0:.1f} bp")
    with pl.Config(tbl_rows=40, tbl_width_chars=200):
        typer.echo(bad if bad.height else r.head(10))


@app.command()
def cancel(sheet_id: str) -> None:
    """Cancel a sheet's open orders."""
    typer.echo(f"cancelled {_run(lambda: _conduit().cancel(sheet_id))} order(s)")


@app.command("cancel-all")
def cancel_all(yes: Annotated[bool, typer.Option("--yes")] = False) -> None:
    """Kill switch: cancel every open order in the account, including ones not from this package."""
    if not yes:
        typer.confirm("Cancel EVERY open order in the account?", abort=True)
    _run(lambda: _conduit().cancel_all())
    typer.echo("global cancel sent")
