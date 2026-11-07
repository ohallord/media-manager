import os
import re
import asyncio
import json
from collections import deque
from datetime import datetime
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from media_manager.database import get_async_db
from media_manager.models import AppLogEvent, FileMoveLog
from media_manager.logger import sse_log_queues

router = APIRouter(prefix="/api/v1/logs", tags=["logs"])

def get_recent_log_file_lines(count: int = 75) -> List[Dict[str, Any]]:
    """
    Reads and parses the last `count` lines from media_manager.log.
    Blocking file I/O - only ever call via asyncio.to_thread from an async handler.
    """
    possible_paths = [
        os.path.join(os.getcwd(), "media_manager.log"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "media_manager.log"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "media_manager.log")
    ]

    log_file = None
    for p in possible_paths:
        if os.path.exists(p):
            log_file = p
            break

    if not log_file:
        return []

    parsed = []
    try:
        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            tail_lines = deque(f, maxlen=count)

        for idx, line in enumerate(tail_lines):
            line_str = line.strip()
            if not line_str:
                continue

            level = "INFO"
            timestamp = datetime.utcnow().isoformat() + "Z"
            message = line_str
            event_tag = "[GENERAL]"

            for lvl in ["WARNING", "ERROR", "INFO", "DEBUG"]:
                pattern = f" - {lvl} - "
                if pattern in line_str:
                    level = lvl
                    parts = line_str.split(pattern, 1)
                    raw_ts = parts[0]
                    try:
                        dt = datetime.strptime(raw_ts, "%Y-%m-%d %H:%M:%S,%f")
                        timestamp = dt.astimezone().isoformat()
                    except Exception:
                        timestamp = raw_ts
                    message = parts[1]
                    break

            tag_match = re.search(r'(\[[A-Z_]+\])', message)
            if tag_match:
                event_tag = tag_match.group(1)

            parsed.append({
                "id": idx + 1,
                "created_at": timestamp,
                "level": level,
                "event_tag": event_tag,
                "message": message,
                "raw": line_str
            })
    except Exception:
        pass

    return parsed

from media_manager.web.api.auth import get_admin_user

@router.get("/recent")
async def get_recent_logs(
    count: int = Query(75, ge=1, le=1000),
    user: Any = Depends(get_admin_user)
):
    """
    Returns the last N log lines (default 75) directly from media_manager.log.
    Requires authentication.
    """
    logs = await asyncio.to_thread(get_recent_log_file_lines, count=count)
    return {
        "count": len(logs),
        "requested_count": count,
        "logs": logs
    }

@router.get("/history")
async def get_log_history(
    level: Optional[str] = Query(None, description="e.g. 'INFO', 'ERROR'"),
    tag: Optional[str] = Query(None, description="e.g. '[COPY_VERIFIED]', '[NEW_MEDIA]'"),
    q: Optional[str] = Query(None),
    limit: int = Query(75, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_async_db),
    user: Any = Depends(get_admin_user)
):
    """
    Returns historical log events from database or log file fallback.
    Requires authentication.
    """
    query = select(AppLogEvent)

    if level:
        query = query.where(AppLogEvent.level == level.upper())
    if tag:
        query = query.where(AppLogEvent.event_tag.ilike(f"%{tag}%"))
    if q:
        query = query.where(AppLogEvent.message.ilike(f"%{q}%"))

    query = query.order_by(AppLogEvent.id.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    events = result.scalars().all()

    if not events and not level and not tag and not q:
        recent_logs = await asyncio.to_thread(get_recent_log_file_lines, count=limit)
        return {
            "total": len(recent_logs),
            "events": recent_logs
        }

    return {
        "total": len(events),
        "events": [
            {
                "id": e.id,
                "level": e.level,
                "event_tag": e.event_tag,
                "message": e.message,
                "created_at": e.created_at.isoformat() + "Z" if e.created_at else None
            }
            for e in events
        ]
    }

@router.get("/moves")
async def get_move_audits(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_async_db),
    user: Any = Depends(get_admin_user)
):
    """
    Returns automated file move audit records from file_move_logs.
    Requires authentication.
    """
    query = select(FileMoveLog).order_by(FileMoveLog.moved_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    moves = result.scalars().all()

    return {
        "moves": [
            {
                "id": m.id,
                "source_path": m.source_path,
                "destination_path": m.destination_path,
                "media_title": m.media_title,
                "quality_before": m.quality_before,
                "quality_after": m.quality_after,
                "copy_verified": m.copy_verified,
                "status": m.status,
                "error_message": m.error_message,
                "moved_at": m.moved_at.isoformat() + "Z" if m.moved_at else None
            }
            for m in moves
        ]
    }

@router.get("/stream")
async def stream_live_logs(
    backfill: int = Query(75, ge=0, le=500),
    user: Any = Depends(get_admin_user)
):
    """
    Establishes a Server-Sent Events (SSE) stream for real-time log push.
    Requires authentication.
    Backfills initial payload with the last N log lines (default 75).
    """
    async def log_event_generator():
        q = asyncio.Queue()
        sse_log_queues.add(q)
        try:
            if backfill > 0:
                recent_logs = await asyncio.to_thread(get_recent_log_file_lines, count=backfill)
                for item in recent_logs:
                    yield f"data: {json.dumps(item)}\n\n"

            yield f"data: {json.dumps({'message': 'Connected to live log stream via SSE', 'level': 'INFO', 'event_tag': '[SYSTEM]'})}\n\n"

            while True:
                log_data = await q.get()
                yield f"data: {json.dumps(log_data)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            sse_log_queues.discard(q)

    return StreamingResponse(log_event_generator(), media_type="text/event-stream")
