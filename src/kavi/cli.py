"""Kavi CLI — entry point for all commands."""

import typer

from kavi import __version__


def version_callback(value: bool) -> None:
    if value:
        typer.echo(f"kavi {__version__}")
        raise typer.Exit()


app = typer.Typer(
    name="kavi",
    help="Governed skill forge for self-building systems.",
    no_args_is_help=True,
)


@app.callback()
def main(
    version: bool | None = typer.Option(  # noqa: N803
        None, "--version", "-V", callback=version_callback, is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Kavi — governed skill forge."""


@app.command()
def status() -> None:
    """Show Kavi status and configuration."""
    from kavi.config import LEDGER_DB, REGISTRY_PATH, VAULT_OUT

    typer.echo(f"kavi {__version__}")
    typer.echo(f"  ledger:   {LEDGER_DB}")
    typer.echo(f"  registry: {REGISTRY_PATH}")
    typer.echo(f"  vault:    {VAULT_OUT}")
