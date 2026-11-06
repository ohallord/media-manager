import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse

from media_manager.config import VERSION
from media_manager.database import init_db_schema
from media_manager.logger import setup_logging
from media_manager.web.api.inventory import router as inventory_router
from media_manager.web.api.auth import router as auth_router
from media_manager.web.api.admin_users import router as admin_users_router
from media_manager.web.api import integrations as integrations_module
from media_manager.web.api.integrations import router as integrations_router
from media_manager.web.api.logs import router as logs_router
from media_manager.web.api.watcher_heartbeat import router as watcher_heartbeat_router
from media_manager.web.api.playback import router as playback_router
from media_manager.web.api.cleanup import router as cleanup_router
from media_manager.web.api.redownload import router as redownload_router
from media_manager.web.api.transcode import router as transcode_router
from media_manager.web.api.transcode_worker import router as transcode_worker_router

logger = setup_logging()

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing Media Manager Web Portal backend...")
    try:
        await init_db_schema()
    except Exception as e:
        logger.warning(f"Could not auto-create DB schema on startup: {e}")

    # One pooled httpx.AsyncClient shared by every Radarr/Sonarr/SABnzbd/TMDb/Fanart/
    # Syncthing client built in integrations.py, instead of each of their ~55 methods
    # opening a fresh client (and TCP/TLS handshake) per call.
    import httpx
    integrations_module.SHARED_HTTP_CLIENT = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        follow_redirects=True,
    )

    # Start the weekly metadata sync background scheduler
    import asyncio
    from media_manager.web.services.metadata_sync import weekly_metadata_sync_scheduler
    from media_manager.web.services.playback_sync import playback_sync_scheduler
    from media_manager.web.services.transcode_scheduler import transcode_scheduler
    sync_task = asyncio.create_task(weekly_metadata_sync_scheduler())
    playback_sync_task = asyncio.create_task(playback_sync_scheduler())
    transcode_task = asyncio.create_task(transcode_scheduler())

    yield

    # Cancel background sync on shutdown
    sync_task.cancel()
    playback_sync_task.cancel()
    transcode_task.cancel()
    await integrations_module.SHARED_HTTP_CLIENT.aclose()
    logger.info("Shutting down Media Manager Web Portal backend.")

app = FastAPI(
    title="Media Manager Web Portal API",
    description="Unified API for Media Inventory, Duplicates, Move Auditing, SSE Logs, and Arr Integrations",
    version=VERSION,
    lifespan=lifespan
)

# Same-origin SPA does not need CORS. Wildcard origins plus credentials is unsafe.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

# Compresses large JSON responses (inventory/upcoming/trending payloads) and static assets.
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Attach API Routers
app.include_router(inventory_router)
app.include_router(auth_router)
app.include_router(admin_users_router)
app.include_router(integrations_router)
app.include_router(logs_router)
app.include_router(watcher_heartbeat_router)
app.include_router(playback_router)
app.include_router(cleanup_router)
app.include_router(redownload_router)
app.include_router(transcode_router)
app.include_router(transcode_worker_router)

# Global Exception Handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled Exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error occurred."}
    )

# Static Files Directory Path
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/api/v1/version")
async def get_version():
    return {"version": VERSION}

@app.get("/")
@app.head("/")
async def serve_index():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return {"message": "Media Manager API running. Frontend index.html not found."}
