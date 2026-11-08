import os
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Response, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from media_manager.database import get_async_db
from media_manager.models import User, PasskeyCredential
from media_manager.config import INITIAL_ADMIN_USER, INITIAL_ADMIN_PASSWORD
from media_manager.web.services.auth import (
    hash_password, verify_password, create_access_token, decode_access_token
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

class LoginRequest(BaseModel):
    username: str
    password: str

async def get_current_user_optional(request: Request, db: AsyncSession = Depends(get_async_db)) -> Optional[User]:
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
    if not token:
        return None

    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        return None

    user_id = payload["sub"]
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        return None
    # Tokens issued before the last password change are revoked.
    if payload.get("tv", 0) != (user.token_version or 0):
        return None
    return user


def cookie_secure(request: Request) -> bool:
    """Mark the cookie Secure when the browser reached us over HTTPS (directly or via the ingress)."""
    mode = (os.getenv("COOKIE_SECURE") or "auto").strip().lower()
    if mode in ("1", "true", "yes", "on"):
        return True
    if mode in ("0", "false", "no", "off"):
        return False
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or proto == "https"


def issue_session(user: User, request: Request, response: Response) -> str:
    """Create the JWT for `user` and set it as the session cookie."""
    token = create_access_token({
        "sub": user.id, "username": user.username, "role": user.role,
        "tv": user.token_version or 0,
    })
    response.set_cookie(
        key="access_token", value=token, httponly=True, samesite="lax",
        secure=cookie_secure(request),
    )
    return token


def revoke_sessions(user: User) -> None:
    """Invalidate every JWT issued to `user` so far."""
    user.token_version = (user.token_version or 0) + 1

async def get_current_user(user: Optional[User] = Depends(get_current_user_optional)) -> User:
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user

async def get_current_active_user(user: User = Depends(get_current_user)) -> User:
    """
    Same as get_current_user, but also enforces a pending forced password
    change server-side. Use this (not get_current_user) for every route
    except the ones a user must still be able to reach while
    must_change_password is set: /auth/change-password itself (the only way
    to clear the flag), /auth/me (so the frontend can even discover the
    flag is set), /auth/logout, and the unauthenticated recovery flows.
    Without this, the flag was purely a client-side UI prompt that any
    direct API call (or a user who just ignores the modal) could bypass
    entirely.
    """
    # getattr, not direct attribute access: some tests override get_current_user
    # with lightweight fakes (dicts, MagicMocks) that don't carry every User
    # field - treat those as "not pending a reset" rather than erroring.
    if getattr(user, "must_change_password", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Password change required before continuing"
        )
    return user

async def get_admin_user(user: User = Depends(get_current_active_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user

import hmac
import secrets
import time
import uuid
import logging

logger = logging.getLogger(__name__)

# One-time codes for 2FA login and password reset. A 6-digit code is only safe
# with a small guess budget and a short lifetime: after MAX_CODE_ATTEMPTS wrong
# guesses (or CODE_TTL_SECONDS) the temp_token is dropped and the flow restarts.
CODE_TTL_SECONDS = 600
MAX_CODE_ATTEMPTS = 5
# Without this, re-requesting a token resets the guess budget. At most
# MAX_CODES_PER_WINDOW codes per (flow, user) per CODE_TTL_SECONDS.
MAX_CODES_PER_WINDOW = 5
CODE_ISSUE_LOG = {}

PENDING_2FA = {}


def _issue_allowed(flow: str, user_id: str) -> bool:
    now = time.monotonic()
    key = (flow, user_id)
    recent = [t for t in CODE_ISSUE_LOG.get(key, []) if now - t < CODE_TTL_SECONDS]
    if len(recent) >= MAX_CODES_PER_WINDOW:
        CODE_ISSUE_LOG[key] = recent
        return False
    recent.append(now)
    CODE_ISSUE_LOG[key] = recent
    return True


def _new_code() -> str:
    return f"{100000 + secrets.randbelow(900000)}"


def _prune_expired(store: dict) -> None:
    now = time.monotonic()
    for token in [t for t, e in store.items() if e["expires_at"] <= now]:
        store.pop(token, None)


def _issue_code(store: dict, user_id: Optional[str]) -> tuple:
    """Store a fresh code under a new temp_token. user_id=None makes a decoy entry
    that can never verify, so unknown accounts look identical to real ones."""
    _prune_expired(store)
    code = _new_code()
    temp_token = str(uuid.uuid4())
    store[temp_token] = {
        "user_id": user_id,
        "code": code,
        "expires_at": time.monotonic() + CODE_TTL_SECONDS,
        "attempts": 0,
    }
    return temp_token, code


def _check_code(store: dict, temp_token: str, code: str, invalid_detail: str, wrong_detail: str) -> dict:
    """Return the pending entry if `code` matches; otherwise count the attempt and raise 400."""
    pending = store.get(temp_token)
    if not pending or pending["expires_at"] <= time.monotonic():
        store.pop(temp_token, None)
        raise HTTPException(status_code=400, detail=invalid_detail)
    matches = hmac.compare_digest(pending["code"].encode(), (code or "").strip().encode())
    if not matches or pending["user_id"] is None:
        pending["attempts"] += 1
        if pending["attempts"] >= MAX_CODE_ATTEMPTS:
            store.pop(temp_token, None)
        raise HTTPException(status_code=400, detail=wrong_detail)
    return pending

class Verify2FARequest(BaseModel):
    temp_token: str
    code: str

@router.post("/login")
async def login(req: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_async_db)):
    result = await db.execute(select(User).where(User.username == req.username))
    user = result.scalar_one_or_none()

    if (
        not user
        and INITIAL_ADMIN_PASSWORD
        and req.username == INITIAL_ADMIN_USER
        and secrets.compare_digest(req.password.encode(), INITIAL_ADMIN_PASSWORD.encode())
    ):
        # Bootstrap admin from env; the env password is a one-time credential,
        # so force a change on first login.
        user = User(
            username=INITIAL_ADMIN_USER,
            password_hash=hash_password(INITIAL_ADMIN_PASSWORD),
            role="admin",
            is_active=True,
            must_change_password=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    if not user or not user.password_hash or not verify_password(user.password_hash, req.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")

    if user.two_factor_enabled:
        if not _issue_allowed("2fa", user.id):
            raise HTTPException(status_code=429, detail="Too many verification codes requested. Try again in a few minutes.")
        temp_token, code = _issue_code(PENDING_2FA, user.id)

        logging.info(f"[2FA_CODE] Verification code for user {user.username} sent via {user.two_factor_method}")
        
        if user.two_factor_method == "email" and user.email:
            from media_manager.web.services.email import send_email_notification
            import asyncio
            asyncio.create_task(send_email_notification(
                user.email,
                "Your 2FA Verification Code",
                f"Hello {user.username},\n\nYour two-factor verification code is: {code}\n\nThis code expires in {CODE_TTL_SECONDS // 60} minutes."
            ))

        return {
            "status": "requires_2fa",
            "two_factor_method": user.two_factor_method,
            "temp_token": temp_token
        }

    user.last_login_at = datetime.utcnow()
    await db.commit()

    token = issue_session(user, request, response)

    return {
        "status": "success",
        "user": {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "must_change_password": user.must_change_password
        },
        "access_token": token
    }

@router.post("/verify-2fa")
async def verify_2fa(req: Verify2FARequest, request: Request, response: Response, db: AsyncSession = Depends(get_async_db)):
    pending = _check_code(
        PENDING_2FA, req.temp_token, req.code,
        "Invalid or expired session.", "Incorrect 2FA verification code.",
    )

    # Code is correct, retrieve user
    user_id = pending["user_id"]
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(status_code=404, detail="User account not found.")

    # Remove code from memory
    PENDING_2FA.pop(req.temp_token, None)

    user.last_login_at = datetime.utcnow()
    await db.commit()

    token = issue_session(user, request, response)

    return {
        "status": "success",
        "user": {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "must_change_password": user.must_change_password
        },
        "access_token": token
    }

@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("access_token")
    return {"message": "Successfully logged out"}

@router.get("/me")
async def get_me(user: Optional[User] = Depends(get_current_user_optional)):
    if not user:
        return {"user": None}
    return {
        "user": {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "must_change_password": user.must_change_password,
            "notify_on_available": bool(getattr(user, "notify_on_available", True)),
            "has_email": bool(user.email),
        }
    }


class NotificationPrefsRequest(BaseModel):
    notify_on_available: bool


@router.put("/me/notifications")
async def update_my_notifications(
    req: NotificationPrefsRequest,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """The current user's own 'email me when my requests are ready' setting."""
    user.notify_on_available = req.notify_on_available
    await db.commit()
    return {"notify_on_available": user.notify_on_available, "has_email": bool(user.email)}

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

@router.post("/change-password")
async def change_password(
    req: ChangePasswordRequest,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    Allows the currently logged in user to change their password.
    """
    if not user.password_hash or not verify_password(user.password_hash, req.current_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    if not req.new_password or len(req.new_password.strip()) < 4:
        raise HTTPException(status_code=400, detail="New password must be at least 4 characters long")

    user.password_hash = hash_password(req.new_password.strip())
    user.must_change_password = False
    revoke_sessions(user)
    await db.commit()
    # Keep this browser signed in; every other session is now revoked.
    issue_session(user, request, response)
    return {"message": "Password changed successfully"}

PENDING_PASSWORD_RESET = {}

class ForgotPasswordRequest(BaseModel):
    username: str

class ResetPasswordSubmitRequest(BaseModel):
    temp_token: str
    code: str
    new_password: str

@router.post("/forgot-password")
async def forgot_password_request(
    req: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_async_db)
):
    # Unknown users and users without contact details get the same response as a
    # real request (a decoy temp_token that never verifies), so this endpoint is
    # not a username or contact-info oracle.
    result = await db.execute(select(User).where(User.username == req.username.strip()))
    user = result.scalar_one_or_none()
    generic = {"status": "requires_verification"}
    if not user:
        temp_token, _ = _issue_code(PENDING_PASSWORD_RESET, None)
        return {**generic, "temp_token": temp_token}

    method = user.two_factor_method if user.two_factor_method != "none" else "email"
    contact_value = user.email if method == "email" else user.phone
    if not contact_value:
        # Fall back to whichever one is configured
        if user.email:
            method = "email"
        elif user.phone:
            method = "phone"
        else:
            logger.info(f"[RESET_PASSWORD_CODE] User {user.username} has no email or phone; reset not possible")
            temp_token, _ = _issue_code(PENDING_PASSWORD_RESET, None)
            return {**generic, "temp_token": temp_token}

    if not _issue_allowed("reset", user.id):
        logger.warning(f"[RESET_PASSWORD_CODE] Rate limit hit for user {user.username}; no code sent")
        temp_token, _ = _issue_code(PENDING_PASSWORD_RESET, None)
        return {**generic, "temp_token": temp_token}
    temp_token, code = _issue_code(PENDING_PASSWORD_RESET, user.id)

    logger.info(f"[RESET_PASSWORD_CODE] Verification code for user {user.username} sent via {method.upper()}")

    if method == "email" and user.email:
        from media_manager.web.services.email import send_email_notification
        import asyncio
        asyncio.create_task(send_email_notification(
            user.email,
            "Your Password Reset Code",
            f"Hello {user.username},\n\nYour password reset verification code is: {code}\n\nIf you did not request this, please ignore this message."
        ))

    return {**generic, "temp_token": temp_token}

@router.post("/reset-password")
async def reset_password_submit(
    req: ResetPasswordSubmitRequest,
    db: AsyncSession = Depends(get_async_db)
):
    pending = _check_code(
        PENDING_PASSWORD_RESET, req.temp_token, req.code,
        "Invalid or expired reset session.", "Incorrect verification code.",
    )

    if not req.new_password or len(req.new_password.strip()) < 4:
        raise HTTPException(status_code=400, detail="New password must be at least 4 characters long.")

    user_id = pending["user_id"]
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User account not found.")

    user.password_hash = hash_password(req.new_password.strip())
    user.must_change_password = False
    revoke_sessions(user)
    await db.commit()
    PENDING_PASSWORD_RESET.pop(req.temp_token, None)
    return {"status": "success", "message": "Your password has been successfully reset."}
