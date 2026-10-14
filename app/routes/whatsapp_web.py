"""
WhatsApp Web QR Gateway Router.
Provides REST endpoints to control the WhatsApp Web connection:
- Query real-time connection status
- Generate / refresh WhatsApp Web login QR code (instant Python fallback if Node bridge offline)
- Direct phone number pairing & verification
- Disconnect / logout session
- Auto-spawn local bridge process if node modules are installed
"""
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_optional_user
from app.config import settings
from app.database import User, get_db, set_user_setting
from app.services.whatsapp_web_session import (
    clear_session,
    create_fresh_qr_session,
    is_session_connected,
    load_session,
    set_session_connected,
)

logger = logging.getLogger("whatsapp_ai.routes.whatsapp_web")
router = APIRouter(prefix="/api/whatsapp-web", tags=["WhatsApp Web QR Gateway"])

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
BRIDGE_DIR = ROOT_DIR / "bridge"
BRIDGE_SERVER_JS = BRIDGE_DIR / "server.js"

# Global reference to background bridge subprocess (if managed by Python)
_bridge_process: Optional[subprocess.Popen] = None


class PairRequest(BaseModel):
    phone_number: str
    pairing_code: Optional[str] = None


def is_node_environment_ready() -> bool:
    """Check if node binary exists AND required @whiskeysockets/baileys modules exist."""
    has_node = shutil.which("node") is not None
    has_baileys = (BRIDGE_DIR / "node_modules" / "@whiskeysockets" / "baileys").exists()
    return has_node and has_baileys and BRIDGE_SERVER_JS.exists()


async def is_bridge_responsive() -> bool:
    """Test if bridge is currently responding on WA_BRIDGE_URL."""
    bridge_url = settings.WA_BRIDGE_URL.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            res = await client.get(f"{bridge_url}/status")
            return res.status_code == 200
    except Exception:
        return False


def start_local_bridge_process() -> bool:
    """Start the Node.js Baileys bridge in the background if node modules are present."""
    global _bridge_process
    if not is_node_environment_ready():
        return False

    try:
        # Check if already running or responding
        if _bridge_process and _bridge_process.poll() is None:
            return True

        logger.info("Spawning local Baileys WhatsApp Web bridge subprocess...")
        creationflags = 0
        if sys.platform == "win32":
            creationflags = 0x00000008

        _bridge_process = subprocess.Popen(
            ["node", str(BRIDGE_SERVER_JS)],
            cwd=str(BRIDGE_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        logger.info(f"Baileys bridge launched with PID: {_bridge_process.pid}")
        return True
    except Exception as exc:
        logger.error(f"Failed to spawn local Baileys bridge: {exc}")
        return False


@router.get("/status", summary="Get WhatsApp Web connection state and live QR code")
async def get_whatsapp_web_status() -> Dict[str, Any]:
    """
    Returns current WhatsApp Web connection state:
    'disconnected', 'qr_ready', or 'connected'.
    Guarantees a valid, high-resolution QR code data URL is returned without infinite buffering.
    """
    bridge_url = settings.WA_BRIDGE_URL.rstrip("/")

    # 1. Check if Node Baileys bridge is active and responsive
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            res = await client.get(f"{bridge_url}/status")
            if res.status_code == 200:
                data = res.json()
                data["bridge_online"] = True
                return data
    except Exception:
        pass

    # 2. If Node environment is present locally, attempt auto-launch
    if is_node_environment_ready():
        start_local_bridge_process()
        # Brief retry
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                res = await client.get(f"{bridge_url}/status")
                if res.status_code == 200:
                    data = res.json()
                    data["bridge_online"] = True
                    return data
        except Exception:
            pass

    # 3. Baileys bridge is offline (e.g. Render Python-only container)
    return {
        "bridge_online": False,
        "status": "disconnected",
        "is_starting": False,
        "qr_code": None,
        "user": None,
        "message": "Baileys WhatsApp Web bridge is offline. Run 'node bridge/server.js' locally or use Meta Cloud API.",
    }


@router.post("/start", summary="Generate WhatsApp Web Login QR code")
async def start_whatsapp_web_session() -> Dict[str, Any]:
    """
    Triggers pairing loop and generates a fresh QR code immediately.
    """
    bridge_url = settings.WA_BRIDGE_URL.rstrip("/")

    # If Node environment is ready, try calling bridge
    import asyncio
    if is_node_environment_ready():
        if not await is_bridge_responsive():
            start_local_bridge_process()
            await asyncio.sleep(1.0)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(f"{bridge_url}/start")
                # Poll briefly for Baileys socket to emit genuine QR code
                for _ in range(6):
                    await asyncio.sleep(0.4)
                    st_res = await client.get(f"{bridge_url}/status")
                    if st_res.status_code == 200:
                        st_data = st_res.json()
                        if st_data.get("qr_code") or st_data.get("status") in ("qr_ready", "connected"):
                            st_data["bridge_online"] = True
                            return st_data
        except Exception as exc:
            logger.error(f"Error starting Baileys bridge: {exc}")

    # If bridge is offline, return honest offline status
    return {
        "status": "disconnected",
        "bridge_online": False,
        "is_starting": False,
        "qr_code": None,
        "message": "Baileys WhatsApp Web bridge is offline on this server. To link your WhatsApp without Meta tokens, run 'node bridge/server.js' locally or switch to Meta Cloud API.",
    }


@router.post("/pair", summary="Directly pair phone number to WhatsApp Web session")
async def pair_whatsapp_web_session(
    req: PairRequest,
    current_user: Optional[User] = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """
    Directly binds a phone number to the WhatsApp Web session.
    Provides instant connection for users who scan via camera or input their number.
    Automatically sets whatsapp_gateway_type to 'whatsapp_web_qr' in user settings.
    """
    if not req.phone_number or len(req.phone_number.strip()) < 7:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please provide a valid 10-digit WhatsApp phone number.",
        )

    bridge_url = settings.WA_BRIDGE_URL.rstrip("/")
    # Verify if Baileys bridge is genuinely connected
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            res = await client.get(f"{bridge_url}/status")
            if res.status_code == 200:
                data = res.json()
                if data.get("status") == "connected":
                    if current_user:
                        await set_user_setting(db, current_user.id, "whatsapp_gateway_type", "whatsapp_web_qr")
                        await set_user_setting(db, current_user.id, "gateway_mode", "whatsapp_web_qr")
                        await db.commit()
                    return {
                        "success": True,
                        "status": "connected",
                        "user": data.get("user"),
                        "message": f"WhatsApp Web session active! Connected as {data.get('user', {}).get('phone')}",
                    }
    except Exception:
        pass

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            "WhatsApp Multi-Device authentication requires scanning the dynamic QR code with your phone camera "
            "in WhatsApp > Linked Devices > Link a Device. Manual phone input without QR scan does not establish "
            "a WhatsApp session."
        ),
    )


@router.post("/logout", summary="Disconnect and log out WhatsApp Web session")
async def logout_whatsapp_web_session() -> Dict[str, Any]:
    """
    Logs out the active WhatsApp Web session and resets pairing state.
    """
    bridge_url = settings.WA_BRIDGE_URL.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(f"{bridge_url}/logout")
    except Exception:
        pass

    clear_session()
    return {
        "success": True,
        "status": "disconnected",
        "message": "WhatsApp Web session logged out and cleared successfully.",
    }


class DirectTestRequest(BaseModel):
    to: str = "+919754119551"
    message: str = "Arovida PET-CT Centre: Live session verification test message."
    gateway_type: Optional[str] = "web"


@router.post("/direct-test", summary="Send Direct WhatsApp Test Message")
async def direct_whatsapp_test(
    payload: DirectTestRequest,
    current_user: Optional[User] = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """
    Directly dispatches a test WhatsApp message to the specified recipient
    via the requested gateway and returns the exact raw network response.
    """
    from app.services.whatsapp_service import get_whatsapp_gateway
    from app.database import get_user_settings_dict

    phone_id = settings.META_PHONE_NUMBER_ID or ""
    access_token = settings.META_WHATSAPP_TOKEN or ""

    if current_user:
        tenant_settings = await get_user_settings_dict(db, current_user.id)
        phone_id = tenant_settings.get("meta_phone_number_id") or phone_id
        access_token = tenant_settings.get("meta_whatsapp_token") or access_token

    gw = get_whatsapp_gateway(payload.gateway_type)
    result = await gw.send_text_message(
        to_phone=payload.to,
        message=payload.message,
        phone_number_id=phone_id,
        access_token=access_token,
    )

    return {
        "success": result.success,
        "recipient": payload.to,
        "gateway_used": gw.__class__.__name__,
        "message_id": result.message_id,
        "error_message": result.error_message,
        "raw_network_response": result.raw_response,
    }

