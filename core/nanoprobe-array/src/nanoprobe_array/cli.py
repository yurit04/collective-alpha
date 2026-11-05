"""`nanoprobe` command line: build the daily intraday summaries and look at intraday bars."""

from __future__ import annotations

import json
import logging
import sys
from typing import Annotated

import typer

app = typer.Typer(help="nanoprobe-array: intraday bars, features and daily intraday summaries.",
                  no_args_is_help=True, pretty_exceptions_show_locals=False)


@app.callback()
def main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    root = logging.getLogger("nanoprobe_array")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not root.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        root.addHandler(h)


@app.command()
def build(years: Annotated[str | None, typer.Option(help="Comma-separated years (default: missing or behind).")] = None,
          workers: Annotated[int, typer.Option(help="Processes, one year each.")] = 4,
          full: Annotated[bool, typer.Option(help="Rebuild every year.")] = False) -> None:
    """Build the daily intraday summaries from the stored minute bars (reads only Massive data)."""
    from ._probe import Nanoprobe

    ys = [int(y) for y in years.split(",")] if years else None
    info = Nanoprobe().build(ys, workers=workers, full=full)
    typer.echo(f"  {len(info['years'])} year(s), {info['days']:,} days, {info['rows']:,} rows, {info['seconds']}s")


@app.command()
def status(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """What the summaries store holds and whether it is behind the minute bars."""
    from ._probe import Nanoprobe

    s = Nanoprobe().status()
    if as_json:
        typer.echo(json.dumps(s, indent=1))
        return
    for k, v in s.items():
        typer.echo(f"  {k:<17} {v}")


@app.command()
def show(ticker: str, day: Annotated[str, typer.Argument(help="Trading date YYYY-MM-DD.")],
         freq: Annotated[str, typer.Option(help="Bar size, e.g. 5min, 30min.")] = "30min") -> None:
    """One ticker's bars and features for a day."""
    import polars as pl

    from ._probe import Nanoprobe

    b = Nanoprobe().bars(ticker, day, freq=freq, fill="grid", prev_close=True,
                         features=["ret_open", "vwap_dev", "rvol", "or_break"])
    with pl.Config(tbl_rows=100, tbl_cols=20, tbl_width_chars=200):
        typer.echo(b.select(pl.col("bar_end").dt.convert_time_zone("America/New_York").dt.strftime("%H:%M").alias("end"),
                            "open", "high", "low", "close", "volume", "stale", "ret_open", "vwap_dev", "rvol", "or_break"))
