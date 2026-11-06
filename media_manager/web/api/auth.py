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
    return user

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

import random
import secrets
import uuid
import logging

logger = logging.getLogger(__name__)

PENDING_2FA = {}

class Verify2FARequest(BaseModel):
    temp_token: str
    code: str

@router.post("/login")
async def login(req: LoginRequest, response: Response, db: AsyncSession = Depends(get_async_db)):
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

    if user.two_factor_enabled:
        code = f"{random.randint(100000, 999999)}"
        temp_token = str(uuid.uuid4())
        
        PENDING_2FA[temp_token] = {
            "user_id": user.id,
            "code": code
        }

        logging.info(f"[2FA_CODE] Verification code for user {user.username} sent via {user.two_factor_method}")
        
        if user.two_factor_method == "email" and user.email:
            from media_manager.web.services.email import send_email_notification
            import asyncio
            asyncio.create_task(send_email_notification(
                user.email,
                "Your 2FA Verification Code",
                f"Hello {user.username},\n\nYour two-factor verification code is: {code}\n\nThis code will expire shortly."
            ))

        return {
            "status": "requires_2fa",
            "two_factor_method": user.two_factor_method,
            "temp_token": temp_token
        }

    user.last_login_at = datetime.utcnow()
    await db.commit()

    token = create_access_token({"sub": user.id, "username": user.username, "role": user.role})
    response.set_cookie(key="access_token", value=token, httponly=True, samesite="lax")

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
async def verify_2fa(req: Verify2FARequest, response: Response, db: AsyncSession = Depends(get_async_db)):
    pending = PENDING_2FA.get(req.temp_token)
    if not pending:
        raise HTTPException(status_code=400, detail="Invalid or expired session.")

    if pending["code"] != req.code.strip():
        raise HTTPException(status_code=400, detail="Incorrect 2FA verification code.")

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

    token = create_access_token({"sub": user.id, "username": user.username, "role": user.role})
    response.set_cookie(key="access_token", value=token, httponly=True, samesite="lax")

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
            "must_change_password": user.must_change_password
        }
    }

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

@router.post("/change-password")
async def change_password(
    req: ChangePasswordRequest,
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
    await db.commit()
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
    result = await db.execute(select(User).where(User.username == req.username.strip()))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Username not found.")

    method = user.two_factor_method if user.two_factor_method != "none" else "email"
    contact_value = user.email if method == "email" else user.phone
    if not contact_value:
        # Fall back to whichever one is configured
        if user.email:
            method = "email"
        elif user.phone:
            method = "phone"
        else:
            raise HTTPException(
                status_code=400,
                detail="This user account does not have a registered email or phone number. Please contact an Administrator to reset your password."
            )

    code = f"{random.randint(100000, 999999)}"
    temp_token = str(uuid.uuid4())
    PENDING_PASSWORD_RESET[temp_token] = {
        "user_id": user.id,
        "code": code
    }

    logger.info(f"[RESET_PASSWORD_CODE] Verification code for user {user.username} sent via {method.upper()}")

    if method == "email" and user.email:
        from media_manager.web.services.email import send_email_notification
        import asyncio
        asyncio.create_task(send_email_notification(
            user.email,
            "Your Password Reset Code",
            f"Hello {user.username},\n\nYour password reset verification code is: {code}\n\nIf you did not request this, please ignore this message."
        ))

    return {
        "status": "requires_verification",
        "method": method,
        "temp_token": temp_token
    }

@router.post("/reset-password")
async def reset_password_submit(
    req: ResetPasswordSubmitRequest,
    db: AsyncSession = Depends(get_async_db)
):
    pending = PENDING_PASSWORD_RESET.get(req.temp_token)
    if not pending:
        raise HTTPException(status_code=400, detail="Invalid or expired reset session.")

    if pending["code"] != req.code.strip():
        raise HTTPException(status_code=400, detail="Incorrect verification code.")

    if not req.new_password or len(req.new_password.strip()) < 4:
        raise HTTPException(status_code=400, detail="New password must be at least 4 characters long.")

    user_id = pending["user_id"]
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User account not found.")

    user.password_hash = hash_password(req.new_password.strip())
    user.must_change_password = False
    await db.commit()
    PENDING_PASSWORD_RESET.pop(req.temp_token, None)
    return {"status": "success", "message": "Your password has been successfully reset."}
