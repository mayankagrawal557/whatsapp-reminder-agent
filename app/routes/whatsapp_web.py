"""
WhatsApp Web QR Gateway Router.
Provides REST endpoints to control the Node.js Baileys bridge:
- Query real-time connection status
- Generate / refresh WhatsApp Web login QR code
- Disconnect / logout session
- Auto-spawn local bridge process if not already running
"""
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.config import settings

logger = logging.getLogger("whatsapp_ai.routes.whatsapp_web")
router = APIRouter(prefix="/api/whatsapp-web", tags=["WhatsApp Web QR Gateway"])

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
BRIDGE_DIR = ROOT_DIR / "bridge"
BRIDGE_SERVER_JS = BRIDGE_DIR / "server.js"

# Global reference to background bridge subprocess (if managed by Python)
_bridge_process: Optional[subprocess.Popen] = None


def is_node_available() -> bool:
    """Check if node binary exists in system PATH."""
    return shutil.which("node") is not None


async def is_bridge_responsive() -> bool:
    """Test if bridge is currently responding on WA_BRIDGE_URL."""
    bridge_url = settings.WA_BRIDGE_URL.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            res = await client.get(f"{bridge_url}/status")
            return res.status_code == 200
    except Exception:
        return False


def start_local_bridge_process() -> bool:
    """Start the Node.js Baileys bridge in the background if node is present."""
    global _bridge_process
    if not is_node_available() or not BRIDGE_SERVER_JS.exists():
        logger.warning("Node.js is not installed or bridge/server.js was not found.")
        return False

    try:
        # Check if already running or responding
        if _bridge_process and _bridge_process.poll() is None:
            return True

        logger.info("Spawning local Baileys WhatsApp Web bridge subprocess...")
        creationflags = 0
        if sys.platform == "win32":
            # DETACHED_PROCESS to run in background without creating visible console window
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
    'disconnected', 'connecting', 'qr_ready', or 'connected'.
    Includes base64 QR code data URL when available, and connected phone number.
    """
    bridge_url = settings.WA_BRIDGE_URL.rstrip("/")

    # Check if responsive
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            res = await client.get(f"{bridge_url}/status")
            if res.status_code == 200:
                data = res.json()
                data["bridge_online"] = True
                return data
    except Exception:
        pass

    # If unresponsive, attempt auto-launch if node is available
    if is_node_available():
        started = start_local_bridge_process()
        return {
            "bridge_online": started,
            "status": "starting" if started else "bridge_offline",
            "is_starting": True,
            "qr_code": None,
            "user": None,
            "message": "Starting background WhatsApp Web bridge. Please wait a few seconds...",
        }

    return {
        "bridge_online": False,
        "status": "bridge_offline",
        "qr_code": None,
        "user": None,
        "message": "Node.js is not detected on host. Please ensure Node.js is installed to run Baileys bridge.",
    }


@router.post("/start", summary="Generate WhatsApp Web Login QR code")
async def start_whatsapp_web_session() -> Dict[str, Any]:
    """
    Triggers Baileys multi-device pairing loop and generates a fresh QR code.
    """
    bridge_url = settings.WA_BRIDGE_URL.rstrip("/")

    # Ensure bridge is running
    if not await is_bridge_responsive():
        start_local_bridge_process()

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            res = await client.post(f"{bridge_url}/start")
            if res.status_code == 200:
                return res.json()
            raise HTTPException(
                status_code=res.status_code,
                detail=f"Bridge returned error: {res.text}",
            )
    except Exception as exc:
        logger.error(f"Error calling /start on bridge: {exc}")
        # Try spawning again
        start_local_bridge_process()
        return {
            "status": "connecting",
            "message": "Bridge is launching. Please check status in a moment for your QR code.",
        }


@router.post("/logout", summary="Disconnect and log out WhatsApp Web session")
async def logout_whatsapp_web_session() -> Dict[str, Any]:
    """
    Logs out the active WhatsApp Web session and clears local session tokens.
    """
    bridge_url = settings.WA_BRIDGE_URL.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            res = await client.post(f"{bridge_url}/logout")
            if res.status_code == 200:
                return res.json()
            return {"success": False, "message": f"Bridge error: {res.text}"}
    except Exception as exc:
        return {"success": False, "message": f"Could not reach bridge: {str(exc)}"}
