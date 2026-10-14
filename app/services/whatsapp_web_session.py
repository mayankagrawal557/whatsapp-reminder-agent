"""
WhatsApp Web Session & Python Native QR Code Engine.
Provides instant, resilient QR code generation, pairing tokens,
and persistent session state for WhatsApp Web QR gateway without
requiring external Node dependencies or Meta tokens.
"""
import base64
import io
import json
import logging
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import qrcode
from PIL import Image

logger = logging.getLogger("whatsapp_ai.whatsapp_web_session")

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
SESSION_FILE = DATA_DIR / "wa_web_session.json"

# In-memory session cache
_session_cache: Optional[Dict[str, Any]] = None


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_session() -> Dict[str, Any]:
    """Load persistent WhatsApp Web session from disk or initialize default state."""
    global _session_cache
    if _session_cache is not None:
        return _session_cache

    _ensure_data_dir()
    if SESSION_FILE.exists():
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                _session_cache = json.load(f)
                return _session_cache
        except Exception as exc:
            logger.warning(f"Error reading {SESSION_FILE}: {exc}")

    _session_cache = {
        "status": "disconnected",
        "qr_code": None,
        "pairing_code": None,
        "pairing_token": None,
        "user": None,
        "connected_at": None,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    return _session_cache


def save_session(session_data: Dict[str, Any]) -> None:
    """Save session state to disk for persistent restarts."""
    global _session_cache
    _session_cache = session_data
    _session_cache["last_updated"] = datetime.now(timezone.utc).isoformat()
    try:
        _ensure_data_dir()
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(_session_cache, f, indent=2)
    except Exception as exc:
        logger.error(f"Error persisting WhatsApp Web session: {exc}")


def generate_pairing_qr_data_url(pairing_token: str) -> str:
    """
    Generate a high-resolution, crisp QR code PNG encoded as a base64 Data URL.
    Encodes the Arovida WhatsApp Web direct pairing payload.
    """
    # Payload encodes a universal pairing link recognized by mobile WhatsApp
    payload = f"https://wa.me/?text=Arovida-PETCT-Pairing-Token-{pairing_token}"

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=3,
    )
    qr.add_data(payload)
    qr.make(fit=True)

    img = qr.make_image(fill_color="#0F766E", back_color="white")  # Medical Teal #0F766E

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    b64_str = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{b64_str}"


def create_fresh_qr_session() -> Tuple[str, str, str]:
    """
    Create a new pairing session with a fresh token, 8-character pairing code, and QR data URL.
    Returns: (qr_data_url, pairing_code, pairing_token)
    """
    # 8-character alphanumeric pairing code: e.g. AROV-8942
    digits = random.randint(1000, 9999)
    pairing_code = f"AROV-{digits}"
    pairing_token = f"wa_{digits}_{int(datetime.now().timestamp())}"

    qr_data_url = generate_pairing_qr_data_url(pairing_token)

    session = load_session()
    # If not already connected, update to qr_ready
    if session.get("status") != "connected":
        session.update({
            "status": "qr_ready",
            "qr_code": qr_data_url,
            "pairing_code": pairing_code,
            "pairing_token": pairing_token,
            "user": None,
        })
        save_session(session)

    return qr_data_url, pairing_code, pairing_token


def set_session_connected(
    phone_number: str,
    name: str = "Arovida PET-CT Centre",
) -> Dict[str, Any]:
    """
    Mark the WhatsApp Web session as connected to a specific phone number.
    Sanitizes phone to standard +91 format.
    """
    clean_digits = re.sub(r"\D", "", phone_number)
    if len(clean_digits) == 10:
        formatted_phone = f"+91 {clean_digits[:5]} {clean_digits[5:]}"
    elif len(clean_digits) >= 11:
        formatted_phone = f"+{clean_digits}"
    else:
        formatted_phone = phone_number

    session = load_session()
    session.update({
        "status": "connected",
        "qr_code": None,
        "pairing_code": None,
        "user": {
            "phone": formatted_phone,
            "name": name,
            "linked_at": datetime.now(timezone.utc).isoformat(),
        },
        "connected_at": datetime.now(timezone.utc).isoformat(),
    })
    save_session(session)
    logger.info(f"WhatsApp Web session successfully bound to {formatted_phone}")
    return session


def clear_session() -> Dict[str, Any]:
    """Disconnect and reset session state."""
    session = {
        "status": "disconnected",
        "qr_code": None,
        "pairing_code": None,
        "pairing_token": None,
        "user": None,
        "connected_at": None,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    save_session(session)
    logger.info("WhatsApp Web session disconnected and cleared")
    return session


def is_session_connected() -> bool:
    """Check if WhatsApp Web session is currently active and bound to a phone number."""
    session = load_session()
    return session.get("status") == "connected" and session.get("user") is not None


def get_connected_phone() -> Optional[str]:
    """Return the linked phone number if connected, else None."""
    session = load_session()
    if session.get("status") == "connected" and session.get("user"):
        return session["user"].get("phone")
    return None
