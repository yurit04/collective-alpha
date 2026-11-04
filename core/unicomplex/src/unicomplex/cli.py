"""`collective` command line. Phase 1: `collective status`."""

from __future__ import annotations

from typing import Annotated

import typer

from ._checks import collect, render, to_json

app = typer.Typer(help="unicomplex: orchestration for the collective.", no_args_is_help=True,
                  pretty_exceptions_show_locals=False)


@app.callback()
def main() -> None:
    """Read-only views across every store; nothing here downloads, builds or trades."""


@app.command()
def status(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """How fresh each store is against the nightly schedule. Exit code 1 when anything is
    stale or missing, so a cron job or a shell prompt can use it."""
    checks = collect()
    typer.echo(to_json(checks) if as_json else render(checks))
    if any(c.state in ("stale", "missing") for c in checks):
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
