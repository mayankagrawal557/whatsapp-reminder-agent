"""
WhatsApp Service: Official WhatsApp Cloud API (Graph API) & Mock Gateway Integration.
Provides reliable message delivery, recipient phone sanitation, credential verification,
and error reporting.
"""
import abc
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional
import httpx

from app.config import settings

logger = logging.getLogger("whatsapp_ai.whatsapp_service")


@dataclass
class WhatsAppSendResult:
    """Outcome of a WhatsApp dispatch attempt."""
    success: bool
    message_id: Optional[str] = None
    error_message: Optional[str] = None
    raw_response: Optional[Dict[str, Any]] = None


class BaseWhatsAppGateway(abc.ABC):
    """Abstract interface for WhatsApp message dispatch providers."""

    @abc.abstractmethod
    async def send_text_message(
        self,
        to_phone: str,
        message: str,
        phone_number_id: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> WhatsAppSendResult:
        """Send a single text message to a recipient phone number."""
        pass

    @abc.abstractmethod
    async def send_interactive_buttons(
        self,
        to_phone: str,
        body_text: str,
        buttons_list: Optional[list] = None,
        phone_number_id: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> WhatsAppSendResult:
        """Send an interactive quick-reply button message to a recipient."""
        pass

    @abc.abstractmethod
    async def test_connection(
        self,
        phone_number_id: str,
        access_token: str,
    ) -> dict:
        """Verify validity of WhatsApp Cloud API credentials."""
        pass


class MetaCloudWhatsAppGateway(BaseWhatsAppGateway):
    """
    Official Meta WhatsApp Cloud API Gateway (Graph API v21.0).
    Sends messages directly via Meta's WhatsApp Business Platform.
    """

    def __init__(self):
        self.default_phone_number_id = settings.META_PHONE_NUMBER_ID
        self.default_token = settings.META_WHATSAPP_TOKEN
        self.graph_version = settings.META_GRAPH_VERSION

    def clean_recipient_phone(self, phone: str) -> str:
        """
        Meta WhatsApp Cloud API expects recipient numbers as purely digits without '+' or spaces.
        e.g., '+1 415-555-2671' -> '14155552671'
        """
        digits = re.sub(r"\D", "", phone)
        # If user forgot country code and entered 10 digits, default country code can be prepended if configured
        if len(digits) == 10 and settings.DEFAULT_COUNTRY_CODE:
            code = re.sub(r"\D", "", settings.DEFAULT_COUNTRY_CODE)
            digits = f"{code}{digits}"
        return digits

    async def send_text_message(
        self,
        to_phone: str,
        message: str,
        phone_number_id: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> WhatsAppSendResult:
        phone_id = (phone_number_id or self.default_phone_number_id or "").strip()
        token = (access_token or self.default_token or "").strip()

        if not phone_id or not token:
            err = (
                "Missing Meta WhatsApp Cloud API credentials. Please set Phone Number ID "
                "and WhatsApp Access Token in Settings."
            )
            logger.error(err)
            return WhatsAppSendResult(success=False, error_message=err)

        recipient_digits = self.clean_recipient_phone(to_phone)
        if not recipient_digits or len(recipient_digits) < 7:
            err = f"Invalid phone number format: '{to_phone}' (extracted digits: '{recipient_digits}')"
            return WhatsAppSendResult(success=False, error_message=err)

        url = f"https://graph.facebook.com/{self.graph_version}/{phone_id}/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_digits,
            "type": "text",
            "text": {
                "preview_url": False,
                "body": message,
            },
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                data = resp.json()

                if resp.status_code in (200, 201):
                    msg_id = data.get("messages", [{}])[0].get("id")
                    logger.info(f"WhatsApp Cloud API successfully dispatched to {recipient_digits}. Msg ID: {msg_id}")
                    return WhatsAppSendResult(success=True, message_id=msg_id, raw_response=data)
                else:
                    error_info = data.get("error", {})
                    err_msg = error_info.get("message", f"HTTP {resp.status_code} Error")
                    err_details = error_info.get("error_data", {}).get("details", "")
                    full_err = f"{err_msg} ({err_details})" if err_details else err_msg
                    logger.error(f"WhatsApp Cloud API Error for {recipient_digits}: {full_err}")
                    return WhatsAppSendResult(success=False, error_message=full_err, raw_response=data)

        except Exception as exc:
            logger.error(f"Failed to communicate with Meta Graph API: {exc}")
            return WhatsAppSendResult(success=False, error_message=f"Network/Connection error: {str(exc)}")

    async def send_interactive_buttons(
        self,
        to_phone: str,
        body_text: str,
        buttons_list: Optional[list] = None,
        phone_number_id: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> WhatsAppSendResult:
        """
        Send WhatsApp interactive quick-reply buttons (Confirm / Reschedule) via Meta Cloud API.
        Automatically falls back to plain text delivery if interactive message cannot be dispatched.
        """
        phone_id = (phone_number_id or self.default_phone_number_id or "").strip()
        token = (access_token or self.default_token or "").strip()

        if not phone_id or not token:
            err = (
                "Missing Meta WhatsApp Cloud API credentials. Please set Phone Number ID "
                "and WhatsApp Access Token in Settings."
            )
            logger.error(err)
            return WhatsAppSendResult(success=False, error_message=err)

        recipient_digits = self.clean_recipient_phone(to_phone)
        if not recipient_digits or len(recipient_digits) < 7:
            err = f"Invalid phone number format: '{to_phone}' (extracted digits: '{recipient_digits}')"
            return WhatsAppSendResult(success=False, error_message=err)

        buttons = buttons_list or [
            {"type": "reply", "reply": {"id": "btn_confirm", "title": "✅ Confirm"}},
            {"type": "reply", "reply": {"id": "btn_reschedule", "title": "📅 Reschedule"}},
        ]

        url = f"https://graph.facebook.com/{self.graph_version}/{phone_id}/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_digits,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body_text},
                "action": {
                    "buttons": buttons,
                },
            },
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                data = resp.json()

                if resp.status_code in (200, 201):
                    msg_id = data.get("messages", [{}])[0].get("id")
                    logger.info(f"WhatsApp Cloud API interactive buttons dispatched to {recipient_digits}. Msg ID: {msg_id}")
                    return WhatsAppSendResult(success=True, message_id=msg_id, raw_response=data)
                else:
                    error_info = data.get("error", {})
                    err_msg = error_info.get("message", f"HTTP {resp.status_code} Error")
                    logger.warning(
                        f"Interactive buttons send error ({err_msg}). Falling back to plain text for {recipient_digits}..."
                    )
                    return await self.send_text_message(to_phone, body_text, phone_number_id, access_token)

        except Exception as exc:
            logger.warning(f"Interactive dispatch error: {exc}. Falling back to plain text delivery...")
            return await self.send_text_message(to_phone, body_text, phone_number_id, access_token)

    async def test_connection(self, phone_number_id: str, access_token: str) -> dict:
        """
        Verify credentials by querying the phone number ID node on Meta Graph API.
        """
        phone_id = phone_number_id.strip()
        token = access_token.strip()

        if not phone_id or not token:
            return {"success": False, "message": "Phone Number ID and Access Token must both be provided."}

        url = f"https://graph.facebook.com/{self.graph_version}/{phone_id}"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"fields": "verified_name,display_phone_number,quality_rating"}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers, params=params)
                data = resp.json()

                if resp.status_code == 200:
                    display_phone = data.get("display_phone_number", phone_id)
                    verified_name = data.get("verified_name", "Verified WhatsApp Account")
                    quality = data.get("quality_rating", "GREEN")
                    return {
                        "success": True,
                        "message": f"Connected to {verified_name} ({display_phone}) - Quality: {quality}",
                        "details": data,
                    }
                else:
                    err = data.get("error", {}).get("message", "Authentication or permission failure")
                    return {"success": False, "message": f"Meta Graph API verification failed: {err}"}
        except Exception as exc:
            return {"success": False, "message": f"Connection test failed: {str(exc)}"}


class MockWhatsAppGateway(BaseWhatsAppGateway):
    """
    Mock WhatsApp Gateway for local testing, dry-runs, and client demonstrations
    without requiring live Meta credentials.
    """

    async def send_text_message(
        self,
        to_phone: str,
        message: str,
        phone_number_id: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> WhatsAppSendResult:
        simulated_id = f"wamid.mock.{uuid.uuid4().hex[:16]}"
        logger.info(f"[MOCK GATEWAY] Simulating WhatsApp dispatch to {to_phone}: {message[:60]}... (ID: {simulated_id})")
        return WhatsAppSendResult(
            success=True,
            message_id=simulated_id,
            raw_response={"simulated": True, "to": to_phone, "status": "sent"},
        )

    async def send_interactive_buttons(
        self,
        to_phone: str,
        body_text: str,
        buttons_list: Optional[list] = None,
        phone_number_id: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> WhatsAppSendResult:
        simulated_id = f"wamid.mock.interactive.{uuid.uuid4().hex[:16]}"
        buttons = buttons_list or [
            {"type": "reply", "reply": {"id": "btn_confirm", "title": "✅ Confirm"}},
            {"type": "reply", "reply": {"id": "btn_reschedule", "title": "📅 Reschedule"}},
        ]
        btn_titles = [b.get("reply", {}).get("title", "") for b in buttons]
        logger.info(
            f"[MOCK GATEWAY] Simulating WhatsApp Interactive Buttons to {to_phone}: "
            f"'{body_text[:60]}...' with Buttons: {btn_titles} (ID: {simulated_id})"
        )
        return WhatsAppSendResult(
            success=True,
            message_id=simulated_id,
            raw_response={
                "simulated": True,
                "type": "interactive",
                "to": to_phone,
                "body": body_text,
                "buttons": buttons,
                "status": "sent",
            },
        )

    async def test_connection(self, phone_number_id: str, access_token: str) -> dict:
        return {
            "success": True,
            "message": "Mock Gateway active. All test messages are simulated successfully without Meta charges.",
        }


def get_whatsapp_gateway(gateway_type: Optional[str] = None) -> BaseWhatsAppGateway:
    """Factory to create configured gateway."""
    mode = (gateway_type or settings.WHATSAPP_GATEWAY_TYPE).lower()
    if mode == "mock":
        return MockWhatsAppGateway()
    return MetaCloudWhatsAppGateway()


async def send_interactive_buttons(
    phone_number: str,
    body_text: str,
    buttons_list: Optional[list] = None,
    access_token: Optional[str] = None,
    phone_number_id: Optional[str] = None,
    gateway_type: Optional[str] = None,
) -> WhatsAppSendResult:
    """
    Top-level helper to send interactive quick-reply buttons.
    """
    gw = get_whatsapp_gateway(gateway_type)
    return await gw.send_interactive_buttons(
        to_phone=phone_number,
        body_text=body_text,
        buttons_list=buttons_list,
        phone_number_id=phone_number_id,
        access_token=access_token,
    )


# Active singleton gateway instance
whatsapp_gateway = get_whatsapp_gateway()
