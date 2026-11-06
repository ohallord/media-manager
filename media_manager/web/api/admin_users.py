from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from media_manager.database import get_async_db
from media_manager.models import User
from media_manager.web.api.auth import get_admin_user, get_current_active_user
from media_manager.web.services.auth import hash_password

router = APIRouter(prefix="/api/v1/admin/users", tags=["admin-users"])

class CreateUserRequest(BaseModel):
    username: str
    password: str
    first_name: str
    last_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    role: Optional[str] = "user"
    two_factor_method: Optional[str] = "none"  # "email", "phone", or "none"

class UpdateUserRoleRequest(BaseModel):
    role: str

class UpdateUserRequest(BaseModel):
    first_name: str
    last_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    role: Optional[str] = None
    two_factor_method: Optional[str] = "none"

@router.get("")
async def list_users(current_user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_async_db)):
    if current_user.role == "admin":
        result = await db.execute(select(User).order_by(User.username.asc()))
        users = result.scalars().all()
    else:
        users = [current_user]
    return {
        "users": [
            {
                "id": u.id,
                "username": u.username,
                "first_name": u.first_name,
                "last_name": u.last_name,
                "email": u.email,
                "phone": u.phone,
                "role": u.role,
                "two_factor_method": u.two_factor_method,
                "two_factor_enabled": u.two_factor_enabled,
                "is_active": u.is_active,
                "created_at": u.created_at.isoformat() + "Z" if u.created_at else None,
                "last_login_at": u.last_login_at.isoformat() + "Z" if u.last_login_at else None,
                "must_change_password": u.must_change_password
            }
            for u in users
        ]
    }

@router.post("")
async def create_user(
    req: CreateUserRequest,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_db)
):
    if not req.first_name.strip():
        raise HTTPException(status_code=400, detail="First name is required.")
    if not req.last_name.strip():
        raise HTTPException(status_code=400, detail="Last name is required.")
    
    email_clean = req.email.strip() if req.email else None
    phone_clean = req.phone.strip() if req.phone else None
    
    if not email_clean and not phone_clean:
        raise HTTPException(status_code=400, detail="Either email or phone number is required.")

    method = req.two_factor_method or "none"
    if method == "email" and not email_clean:
        raise HTTPException(status_code=400, detail="Email is required to enable Email Two-Factor Authentication.")
    if method == "phone" and not phone_clean:
        raise HTTPException(status_code=400, detail="Phone number is required to enable SMS Two-Factor Authentication.")

    result = await db.execute(select(User).where(User.username == req.username.strip()))
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")

    new_user = User(
        username=req.username.strip(),
        password_hash=hash_password(req.password),
        first_name=req.first_name.strip(),
        last_name=req.last_name.strip(),
        email=email_clean,
        phone=phone_clean,
        role=req.role if req.role in ["admin", "user"] else "user",
        two_factor_method=method,
        two_factor_enabled=(method != "none"),
        is_active=True,
        must_change_password=True
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    return {
        "user": {
            "id": new_user.id,
            "username": new_user.username,
            "role": new_user.role
        }
    }

class ResetUserPasswordRequest(BaseModel):
    new_password: str

@router.put("/{user_id}/role")
async def update_user_role(
    user_id: str,
    req: UpdateUserRoleRequest,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_db)
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Guardrail: Admin accounts cannot be demoted to a lower permissions level
    if user.role == "admin" and req.role != "admin":
        raise HTTPException(
            status_code=400,
            detail="Admin accounts cannot be moved to a lower permissions level."
        )

    user.role = req.role if req.role in ["admin", "user"] else "user"
    await db.commit()
    return {"message": "User role updated successfully"}

@router.put("/{user_id}/password")
async def admin_reset_user_password(
    user_id: str,
    req: ResetUserPasswordRequest,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    Allows an Admin to reset any user's password directly from the Admin console.
    """
    if not req.new_password or len(req.new_password.strip()) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters long")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.password_hash = hash_password(req.new_password.strip())
    user.must_change_password = True
    await db.commit()
    return {"message": f"Password for user '{user.username}' reset successfully"}

@router.put("/{user_id}")
async def update_user(
    user_id: str,
    req: UpdateUserRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Guard: must be admin OR the user themselves
    if current_user.role != "admin" and current_user.id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized to edit this user's information")

    if not req.first_name.strip():
        raise HTTPException(status_code=400, detail="First name is required.")
    if not req.last_name.strip():
        raise HTTPException(status_code=400, detail="Last name is required.")
    
    email_clean = req.email.strip() if req.email else None
    phone_clean = req.phone.strip() if req.phone else None
    
    if not email_clean and not phone_clean:
        raise HTTPException(status_code=400, detail="Either email or phone number is required.")

    method = req.two_factor_method or "none"
    if method == "email" and not email_clean:
        raise HTTPException(status_code=400, detail="Email is required to enable Email Two-Factor Authentication.")
    if method == "phone" and not phone_clean:
        raise HTTPException(status_code=400, detail="Phone number is required to enable SMS Two-Factor Authentication.")

    user.first_name = req.first_name.strip()
    user.last_name = req.last_name.strip()
    user.email = email_clean
    user.phone = phone_clean
    user.two_factor_method = method
    user.two_factor_enabled = (method != "none")

    # Only admin can edit role
    if req.role is not None and current_user.role == "admin":
        if user.role == "admin" and req.role != "admin":
            # Check if there are other admin users
            admins_result = await db.execute(select(User).where(User.role == "admin"))
            admins = admins_result.scalars().all()
            if len(admins) <= 1:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot demote the last remaining admin account."
                )
            raise HTTPException(
                status_code=400,
                detail="Admin accounts cannot be moved to a lower permissions level."
            )
        if req.role in ["admin", "user"]:
            user.role = req.role

    await db.commit()
    return {"message": "User information updated successfully"}

@router.delete("/{user_id}")
async def delete_user(
    user_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Guard: must be admin OR the user themselves
    if current_user.role != "admin" and current_user.id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this user")

    if user.role == "admin":
        admins_result = await db.execute(select(User).where(User.role == "admin"))
        admins = admins_result.scalars().all()
        if len(admins) <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the last remaining admin account.")

    await db.delete(user)
    await db.commit()
    return {"message": "User deleted successfully"}

