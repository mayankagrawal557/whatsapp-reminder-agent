"""
Settings API routes: enables clinics to configure WhatsApp credentials,
Gemini API keys, and business profiles isolated to their own tenant account.
"""
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.database import (
    User,
    get_db,
    get_user_settings_dict,
    set_user_setting,
)
from app.schemas import SettingsPayload, SettingsResponse
from app.services.gemini_service import gemini_service
from app.services.whatsapp_service import (
    MetaCloudWhatsAppGateway,
    MockWhatsAppGateway,
)

logger = logging.getLogger("whatsapp_ai.routes.settings")
router = APIRouter(prefix="/api/settings", tags=["Settings"])


def mask_token(token: Optional[str]) -> str:
    """Mask token string for safe dashboard display."""
    if not token or len(token.strip()) < 8:
        return ""
    token = token.strip()
    return f"{token[:4]}...{token[-4:]}"


@router.get("", response_model=SettingsResponse, summary="Get active clinic settings")
async def get_settings(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Fetch all currently configured settings for the authenticated clinic tenant.
    """
    stored = await get_user_settings_dict(db, current_user.id)

    phone_id = stored.get("meta_phone_number_id") or settings.META_PHONE_NUMBER_ID or ""
    raw_token = stored.get("meta_whatsapp_token") or settings.META_WHATSAPP_TOKEN or ""
    raw_gemini_key = stored.get("gemini_api_key") or settings.GEMINI_API_KEY or ""
    gemini_model = stored.get("gemini_model") or settings.GEMINI_MODEL or "gemini-1.5-flash"
    business_name = stored.get("business_name") or current_user.business_name or settings.DEFAULT_BUSINESS_NAME
    business_type = stored.get("business_type") or current_user.business_type or settings.DEFAULT_BUSINESS_TYPE
    default_tone = stored.get("default_tone") or "friendly"
    gateway_type = stored.get("whatsapp_gateway_type") or settings.WHATSAPP_GATEWAY_TYPE
    default_country_code = stored.get("default_country_code") or settings.DEFAULT_COUNTRY_CODE or "+91"
    logo_url = current_user.logo_url or stored.get("logo_url") or None
    brand_color = current_user.brand_color or stored.get("brand_color") or "#10b981"

    return SettingsResponse(
        meta_phone_number_id=phone_id,
        meta_whatsapp_token_masked=mask_token(raw_token),
        gemini_api_key_masked=mask_token(raw_gemini_key),
        gemini_model=gemini_model,
        business_name=business_name,
        business_type=business_type,
        default_tone=default_tone,
        whatsapp_gateway_type=gateway_type,
        default_country_code=default_country_code,
        logo_url=logo_url,
        brand_color=brand_color,
        is_meta_configured=bool(phone_id and raw_token),
        is_gemini_configured=bool(raw_gemini_key),
    )


@router.post("", summary="Save clinic settings")
async def update_settings(
    payload: SettingsPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Persist WhatsApp credentials, Gemini key, and business information isolated to this clinic.
    """
    stored = await get_user_settings_dict(db, current_user.id)

    phone_id = payload.meta_phone_number_id.strip() if payload.meta_phone_number_id else ""
    token = payload.meta_whatsapp_token.strip() if payload.meta_whatsapp_token else ""
    gemini_key = payload.gemini_api_key.strip() if payload.gemini_api_key else ""

    if token and "..." in token:
        token = stored.get("meta_whatsapp_token", "")
    if gemini_key and "..." in gemini_key:
        gemini_key = stored.get("gemini_api_key", "")

    await set_user_setting(db, current_user.id, "meta_phone_number_id", phone_id)
    if token:
        await set_user_setting(db, current_user.id, "meta_whatsapp_token", token)
    if gemini_key:
        await set_user_setting(db, current_user.id, "gemini_api_key", gemini_key)

    await set_user_setting(db, current_user.id, "gemini_model", payload.gemini_model or "gemini-1.5-flash")
    await set_user_setting(db, current_user.id, "business_name", payload.business_name or current_user.business_name)
    await set_user_setting(db, current_user.id, "business_type", payload.business_type or current_user.business_type)
    await set_user_setting(db, current_user.id, "default_tone", payload.default_tone or "friendly")
    await set_user_setting(db, current_user.id, "whatsapp_gateway_type", payload.whatsapp_gateway_type or "meta")
    await set_user_setting(db, current_user.id, "default_country_code", payload.default_country_code or "+91")

    # Update User record business & branding info
    if payload.business_name:
        current_user.business_name = payload.business_name.strip()
    if payload.business_type:
        current_user.business_type = payload.business_type.strip()
    if payload.logo_url is not None:
        current_user.logo_url = payload.logo_url.strip() or None
        await set_user_setting(db, current_user.id, "logo_url", current_user.logo_url)
    if payload.brand_color is not None:
        current_user.brand_color = payload.brand_color.strip() or "#10b981"
        await set_user_setting(db, current_user.id, "brand_color", current_user.brand_color)

    await db.commit()
    await db.refresh(current_user)

    logger.info(f"Clinic [{current_user.id}] settings & branding saved successfully.")
    return {
        "status": "success",
        "message": "Settings updated successfully for your clinic.",
        "user": {
            "id": current_user.id,
            "business_name": current_user.business_name,
            "email": current_user.email,
            "business_type": current_user.business_type,
            "logo_url": current_user.logo_url,
            "brand_color": current_user.brand_color,
        }
    }


@router.post("/branding", summary="Save clinic white-label branding specifically")
async def update_branding_only(
    payload: SettingsPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Dedicated endpoint to update Clinic Display Name, Logo URL, and Brand Color.
    """
    if payload.business_name:
        current_user.business_name = payload.business_name.strip()
        await set_user_setting(db, current_user.id, "business_name", current_user.business_name)
    if payload.business_type:
        current_user.business_type = payload.business_type.strip()
        await set_user_setting(db, current_user.id, "business_type", current_user.business_type)
    if payload.logo_url is not None:
        current_user.logo_url = payload.logo_url.strip() or None
        await set_user_setting(db, current_user.id, "logo_url", current_user.logo_url)
    if payload.brand_color is not None:
        current_user.brand_color = payload.brand_color.strip() or "#10b981"
        await set_user_setting(db, current_user.id, "brand_color", current_user.brand_color)

    await db.commit()
    await db.refresh(current_user)

    return {
        "status": "success",
        "message": "Branding updated successfully.",
        "user": {
            "id": current_user.id,
            "business_name": current_user.business_name,
            "email": current_user.email,
            "business_type": current_user.business_type,
            "logo_url": current_user.logo_url,
            "brand_color": current_user.brand_color,
        }
    }


@router.post("/test-whatsapp", summary="Test WhatsApp Cloud API credentials")
async def test_whatsapp_connection(
    payload: SettingsPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stored = await get_user_settings_dict(db, current_user.id)
    phone_id = payload.meta_phone_number_id or stored.get("meta_phone_number_id") or settings.META_PHONE_NUMBER_ID or ""
    token = payload.meta_whatsapp_token or stored.get("meta_whatsapp_token") or settings.META_WHATSAPP_TOKEN or ""
    gateway_type = payload.whatsapp_gateway_type or stored.get("whatsapp_gateway_type") or settings.WHATSAPP_GATEWAY_TYPE

    if gateway_type == "mock":
        mock_gw = MockWhatsAppGateway()
        return await mock_gw.test_connection(phone_id, token)

    if not phone_id or not token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Both Phone Number ID and WhatsApp Access Token must be provided.",
        )

    meta_gw = MetaCloudWhatsAppGateway()
    result = await meta_gw.test_connection(phone_id, token)
    return result


@router.post("/test-gemini", summary="Test Google Gemini API key")
async def test_gemini_connection(
    payload: SettingsPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stored = await get_user_settings_dict(db, current_user.id)
    api_key = payload.gemini_api_key or stored.get("gemini_api_key") or settings.GEMINI_API_KEY or ""
    model = payload.gemini_model or stored.get("gemini_model") or settings.GEMINI_MODEL or "gemini-1.5-flash"

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Gemini API Key is required to test connection.",
        )

    result = await gemini_service.test_connection(api_key, model)
    return result
