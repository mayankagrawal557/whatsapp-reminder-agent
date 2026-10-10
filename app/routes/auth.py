"""
Authentication routes: Register new clinic/salon tenant, Login, Session status, and Logout.
"""
import uuid
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import create_access_token, get_current_user, hash_password, verify_password
from app.database import User, get_db, set_user_setting

logger = logging.getLogger("whatsapp_ai.routes.auth")
router = APIRouter(prefix="/api/auth", tags=["Authentication"])


class RegisterPayload(BaseModel):
    business_name: str = Field(..., min_length=2, max_length=128, description="Name of the clinic or salon")
    email: str = Field(..., description="Unique login email address")
    password: str = Field(..., min_length=6, description="Account password")
    business_type: Optional[str] = Field("Clinic / Service", description="Type of practice or salon")


class LoginPayload(BaseModel):
    email: str = Field(..., description="Registered email address")
    password: str = Field(..., description="Account password")


class UserResponse(BaseModel):
    id: str
    business_name: str
    email: str
    business_type: str
    is_active: bool

    class Config:
        from_attributes = True


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new clinic or salon tenant",
)
async def register_clinic(payload: RegisterPayload, response: Response, db: AsyncSession = Depends(get_db)):
    """
    Onboard a new clinic/salon tenant with isolated data and credentials.
    """
    email_clean = payload.email.lower().strip()
    stmt = select(User).where(User.email == email_clean)
    res = await db.execute(stmt)
    if res.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email address already exists. Please log in.",
        )

    user_id = f"user_{uuid.uuid4().hex[:12]}"
    hashed_pw = hash_password(payload.password)

    new_user = User(
        id=user_id,
        business_name=payload.business_name.strip(),
        email=email_clean,
        password_hash=hashed_pw,
        business_type=payload.business_type or "Clinic / Service",
        is_active=True,
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    # Seed initial user settings
    await set_user_setting(db, user_id, "business_name", new_user.business_name)
    await set_user_setting(db, user_id, "business_type", new_user.business_type)
    await set_user_setting(db, user_id, "default_tone", "friendly")

    # Generate JWT Token
    token = create_access_token({"sub": new_user.id, "email": new_user.email})
    response.set_cookie(key="access_token", value=token, httponly=True, max_age=604800, samesite="lax")

    logger.info(f"Registered new clinic tenant: {new_user.id} ({new_user.business_name})")
    return AuthResponse(access_token=token, user=new_user)


@router.post(
    "/login",
    response_model=AuthResponse,
    summary="Log in to clinic dashboard",
)
async def login_clinic(payload: LoginPayload, response: Response, db: AsyncSession = Depends(get_db)):
    """
    Authenticate clinic credentials and return JWT session token.
    """
    email_clean = payload.email.lower().strip()
    stmt = select(User).where(User.email == email_clean)
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()

    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is currently deactivated.",
        )

    token = create_access_token({"sub": user.id, "email": user.email})
    response.set_cookie(key="access_token", value=token, httponly=True, max_age=604800, samesite="lax")

    logger.info(f"Clinic logged in: {user.id} ({user.business_name})")
    return AuthResponse(access_token=token, user=user)


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Get current logged in clinic profile",
)
async def get_me(current_user: User = Depends(get_current_user)):
    """
    Fetch the currently active authenticated user session.
    """
    return current_user


@router.post(
    "/logout",
    summary="Log out and clear session cookie",
)
async def logout(response: Response):
    """
    Clear access token cookie to terminate session.
    """
    response.delete_cookie(key="access_token")
    return {"message": "Logged out successfully."}
