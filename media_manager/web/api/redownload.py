import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from media_manager.database import get_async_db
from media_manager.models import User
from media_manager.web.api.auth import get_admin_user, get_current_active_user
from media_manager.web.services.empty_file_redownload import plan_redownload, run_redownload

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/redownload",
    tags=["redownload"],
    # Admin-only: cleanup/migration expose library paths and destructive actions.
    dependencies=[Depends(get_admin_user)],
)


class RedownloadRequest(BaseModel):
    media_item_ids: Optional[List[str]] = None
    all: bool = False


@router.get("/empty-files")
async def preview_empty_files(
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(get_current_active_user),
):
    """0-byte library files and what a re-download would do for each."""
    plan = await plan_redownload(db)
    return {"count": len(plan["items"]), "items": plan["items"]}


@router.post("/empty-files")
async def redownload_empty_files(
    req: RedownloadRequest,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(get_admin_user),
):
    """Deletes stale 0-byte file records in Radarr/Sonarr and searches again (admin only)."""
    if not req.all and not req.media_item_ids:
        raise HTTPException(status_code=400, detail="Pass media_item_ids, or all=true.")
    return await run_redownload(db, None if req.all else req.media_item_ids)
