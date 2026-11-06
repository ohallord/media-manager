import click
import logging
import uvicorn
from media_manager.database import init_db_schema_sync
from media_manager.logger import setup_logging
from media_manager.scanner import scan_all
from media_manager.watcher import start_watching
from media_manager.config import WEB_HOST, WEB_PORT

# Commands that must not touch the local DB (the transcode worker talks to
# the web app over HTTP; this checkout's media.db is not the prod DB).
NO_DB_COMMANDS = {"transcode-worker"}


@click.group()
@click.pass_context
def cli(ctx):
    """Media Manager CLI for inventory management, duplicate detection, and web portal serving."""
    if ctx.invoked_subcommand in NO_DB_COMMANDS:
        return
    setup_logging()
    try:
        init_db_schema_sync()
    except Exception as e:
        logging.warning(f"Could not auto-initialize DB schema: {e}")

@cli.command()
def scan():
    """Scan all media directories and update the PostgreSQL database."""
    logging.info("Starting media inventory scan...")
    scan_all()
    logging.info("Scan complete.")

@cli.command(name="sync-metadata")
def sync_metadata():
    """Sync and cache TMDb media metadata/cast/trailers for all inventory items."""
    import asyncio
    from media_manager.web.services.metadata_sync import run_metadata_sync
    logging.info("Starting manual metadata sync...")
    asyncio.run(run_metadata_sync())
    logging.info("Metadata sync complete.")

@cli.command(name="sync-playback")
def sync_playback():
    """Sync play counts/last-played timestamps from Plex, Emby, and Jellyfin."""
    import asyncio
    from media_manager.web.services.playback_sync import run_playback_sync
    logging.info("Starting manual playback sync...")
    summary = asyncio.run(run_playback_sync())
    logging.info(f"Playback sync complete: {summary}")

@cli.command(name="redownload-empty")
@click.option("--yes", is_flag=True, help="Actually trigger downloads (default is a dry-run preview).")
def redownload_empty(yes):
    """Re-download every movie/episode whose library file is 0 bytes."""
    import asyncio
    from media_manager.database import AsyncSessionLocal
    from media_manager.web.services.empty_file_redownload import plan_redownload, run_redownload

    async def run():
        async with AsyncSessionLocal() as db:
            if yes:
                return (await run_redownload(db))["results"]
            return (await plan_redownload(db))["items"]

    entries = asyncio.run(run())
    for entry in entries:
        click.echo(f"{entry['status']:<18} {entry['path']}  {entry['detail']}")
    counts = {}
    for entry in entries:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    click.echo(f"{'Done' if yes else 'Dry run (pass --yes to trigger)'}: {counts}")

@cli.command(name="transcode-worker")
@click.option("--once", is_flag=True, help="Process at most one job, then exit.")
def transcode_worker(once):
    """Run the host H.265 transcode worker (config from TRANSCODE_* env vars)."""
    from media_manager.transcode.worker import main as worker_main
    raise SystemExit(worker_main(["--once"] if once else []))

@cli.command()
def watch():
    """Start the file system watcher for source directories."""
    logging.info("Starting automated watcher...")
    start_watching()

@cli.command()
@click.option('--host', default=WEB_HOST, help="Host interface to bind")
@click.option('--port', default=WEB_PORT, help="Port to listen on")
def serve(host, port):
    """Start the Media Manager Web Portal server."""
    logging.info(f"Starting Web Portal server at http://{host}:{port}")
    uvicorn.run("media_manager.web.app:app", host=host, port=port, reload=False)

@cli.command()
def test():
    """Run automated test suite for inventory, caching, and API stability."""
    import unittest
    logging.info("Running automated test suite...")
    loader = unittest.TestLoader()
    suite = loader.discover('tests', pattern='test_*.py', top_level_dir='.')
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        raise click.ClickException("Automated tests failed!")

@cli.command()
@click.option('--username', default='admin', help="Username to reset")
@click.option('--password', required=True, prompt=True, hide_input=True, help="New password for user")
def reset_password(username, password):
    """Reset a user's password and ensure admin role."""
    from media_manager.database import SyncSessionLocal
    from media_manager.models import User
    from media_manager.web.services.auth import hash_password

    session = SyncSessionLocal()
    try:
        user = session.query(User).filter(User.username == username).first()
        if not user:
            user = User(
                username=username,
                password_hash=hash_password(password),
                role="admin",
                is_active=True
            )
            session.add(user)
            click.echo(f"Created new admin user '{username}' successfully.")
        else:
            user.password_hash = hash_password(password)
            user.role = "admin"
            user.is_active = True
            click.echo(f"Password reset and admin role restored for user '{username}'.")
        session.commit()
    except Exception as e:
        session.rollback()
        click.echo(f"Error resetting password: {e}")
    finally:
        session.close()

if __name__ == "__main__":
    cli()
