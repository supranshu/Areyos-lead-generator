import logging
from pathlib import Path

import typer

app = typer.Typer(help="Areyos lead generation platform", no_args_is_help=True)
ROOT = Path(__file__).resolve().parent.parent


@app.callback()
def _setup(verbose: bool = typer.Option(False, "--verbose", "-v")):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@app.command("migrate")
def migrate():
    """Apply database migrations (alembic upgrade head)."""
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False):
    """Run the web UI and REST API."""
    import uvicorn

    uvicorn.run("areyos.api.app:app", host=host, port=port, reload=reload)


@app.command()
def worker():
    """Process discovery and enrichment jobs. Run several for more throughput."""
    from areyos.worker import run_forever

    run_forever()


@app.command()
def discover(
    state: str = typer.Option(...),
    city: str = typer.Option(...),
    industry: list[str] = typer.Option([], help="Repeatable; default all industries"),
    provider: list[str] = typer.Option([], help="google_places and/or osm; default all configured"),
    wait: bool = typer.Option(False, help="Process the run's jobs in this process instead of waiting for workers"),
):
    """Start a discovery run."""
    from areyos import engine
    from areyos.db import session_scope
    from areyos.worker import drain

    with session_scope() as session:
        try:
            run = engine.create_run(session, state=state, city=city, industries=industry, providers=provider)
        except engine.RunValidationError as exc:
            raise typer.BadParameter(str(exc)) from exc
        run_id = run.id
    typer.echo(f"run {run_id} queued")
    if wait:
        drain()
        with session_scope() as session:
            from areyos.models import DiscoveryRun

            run = session.get(DiscoveryRun, run_id)
            typer.echo(f"run {run_id}: {run.status} {engine.run_stats(session, run_id)}")
            if run.error:
                typer.echo(f"error: {run.error}", err=True)


if __name__ == "__main__":
    app()
