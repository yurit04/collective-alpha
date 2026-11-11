"""`collective` command line: `status` (read-only), `run` (the nightly steps in order),
`runs` (history) and `service` (the launchd job)."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from ._checks import collect, render, to_json

app = typer.Typer(help="unicomplex: orchestration for the collective.", no_args_is_help=True,
                  pretty_exceptions_show_locals=False)
service_app = typer.Typer(help="The nightly run as a launchd job (macOS).", no_args_is_help=True)
app.add_typer(service_app, name="service")


def _names(value: str | None) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()] if value else []


def _fail(message: str, code: int = 1) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


@app.callback()
def main() -> None:
    """`status` only reads; `run` starts each package's own sync and build commands in order."""


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


@app.command()
def run(
    only: Annotated[str | None, typer.Option(help="Comma-separated steps to run (default: all).")] = None,
    skip: Annotated[str | None, typer.Option(help="Comma-separated steps to leave out.")] = None,
    force: Annotated[bool, typer.Option(help="Run even if a full run succeeded in the last few hours.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the steps and commands; run nothing.")] = False,
    quiet: Annotated[bool, typer.Option(help="Print only progress lines (the log has everything).")] = False,
) -> None:
    """Run the nightly steps one after another: massive, edgar, finra, vinculum, nanoprobe, queen.
    A failed step does not stop the rest; the exit code is 1 if any step failed. A second run
    while one holds the lock exits 0 without doing anything."""
    from ._runner import Runner

    runner = Runner()
    try:
        res = runner.run(_names(only), _names(skip), force=force, dry_run=dry_run, echo=typer.echo, quiet=quiet)
    except ValueError as exc:
        _fail(str(exc), 2)
    if res.status == "dry-run":
        for st in res.steps:
            typer.echo(f"  {st.name:<10} {' '.join(st.command)}   # {st.detail}")
        typer.echo(f"log and history under {runner.root}")
    elif res.status in ("skipped", "locked"):
        typer.echo(res.detail)
    else:
        typer.echo(f"log: {res.log}")
        from ._notify import after_run

        after_run(res, runner.settings, echo=typer.echo)
    raise typer.Exit(res.exit_code)


@app.command()
def runs(
    n: Annotated[int, typer.Option("-n", help="How many recent runs.")] = 10,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Recent runs, newest last: status, steps and how long each took."""
    from ._runner import Runner

    rows = Runner().history(n)
    if as_json:
        typer.echo(json.dumps(rows, indent=1))
        return
    if not rows:
        typer.echo("no runs recorded yet")
        return
    for r in rows:
        steps = " ".join(f"{s['name']}:{s['status']}({s.get('seconds', 0) / 60:.0f}m)" for s in r.get("steps", []))
        scope = "" if r.get("full") else " [partial]"
        typer.echo(f"{r.get('run_id')}  {r.get('status'):<11}{scope}  {steps}")


@service_app.command("install")
def service_install(
    replace: Annotated[bool, typer.Option(
        help="First remove the per-package sync/build schedules (never the Massive stream).")] = False,
) -> None:
    """Install the launchd job: `collective run` Tue-Sat at the configured time (06:30)."""
    from . import _services
    from ._config import load_settings

    s = load_settings()
    try:
        path, removed = _services.install(s, replace=replace)
    except RuntimeError as exc:
        _fail(str(exc))
    for name in removed:
        typer.echo(f"removed the old {name} schedule")
    typer.secho(f"installed {_services.LABEL}: {path} (Tue-Sat {s.run_hour:02d}:{s.run_minute:02d})",
                fg=typer.colors.GREEN)


@service_app.command("remove")
def service_remove() -> None:
    """Unload and delete the launchd job (the per-package schedules are not reinstalled)."""
    from . import _services

    typer.echo("removed" if _services.remove() else "was not installed")


@service_app.command("status")
def service_status() -> None:
    """Show the launchd job's state and any per-package schedules still installed."""
    from . import _services

    typer.echo(json.dumps(_services.status(), indent=1))


if __name__ == "__main__":
    app()
