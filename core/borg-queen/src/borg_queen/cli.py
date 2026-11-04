"""`queen` command line: build the risk model and look at it."""

from __future__ import annotations

import json
import logging
import sys
from typing import Annotated

import typer

app = typer.Typer(help="borg-queen: factor risk model (and, later, portfolio construction).",
                  no_args_is_help=True, pretty_exceptions_show_locals=False)


@app.callback()
def main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    root = logging.getLogger("borg_queen")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not root.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        root.addHandler(h)


@app.command()
def build(start: Annotated[str, typer.Option(help="First month of exposures.")] = "2011-01-01",
          top: Annotated[int, typer.Option(help="Estimation universe: top N companies.")] = 1000) -> None:
    """Estimate the risk model from `start` and write it under the data root."""
    from ._model import RiskModel, RiskSettings

    info = RiskModel().build(start, settings=RiskSettings(top=top))
    typer.echo(f"  {info['days']:,} days ({info['first_month']} to {info['last_month']}), "
               f"median R2 {info['median_r2']:.3f}, {info['seconds']}s")


@app.command()
def status() -> None:
    """When the risk model was built and with what settings."""
    from ._model import RiskModel

    typer.echo(json.dumps(RiskModel().status(), indent=2))


@app.command()
def check() -> None:
    """Forecast against realized risk on test portfolios; the market factor against SPY."""
    from ._model import RiskModel

    results = RiskModel().check()
    for name, ok, detail in results:
        typer.secho(f"  {'pass' if ok else 'FAIL'}    {name}  ({detail})", fg=None if ok else typer.colors.RED)
    if not all(ok for _, ok, _ in results):
        raise typer.Exit(1)
