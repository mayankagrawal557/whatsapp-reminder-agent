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


def get_greenapi_host(id_instance: str) -> str:
    """Resolve Green-API host matching instance prefix (e.g. 7107 -> 7107.api.greenapi.com)."""
    if settings.GREENAPI_HOST:
        return settings.GREENAPI_HOST.strip().replace("https://", "").replace("http://", "").rstrip("/")
    inst = str(id_instance or "").strip()
    prefix = inst[:4] if len(inst) >= 4 else "7107"
    return f"{prefix}.api.greenapi.com"


def send_greenapi_message(phone: str, message: str, id_instance: str, token: str) -> dict:
    """
    Dedicated synchronous Green-API message delivery function.
    Cleans phone, formats chatId as {digits}@c.us, and sends text message.
    """
    clean = re.sub(r"\D", "", phone)
    if len(clean) == 10 and settings.DEFAULT_COUNTRY_CODE:
        code = re.sub(r"\D", "", settings.DEFAULT_COUNTRY_CODE)
        clean = f"{code}{clean}"
    chat_id = f"{clean}@c.us"
    host = get_greenapi_host(id_instance)
    url = f"https://{host}/waInstance{id_instance}/sendMessage/{token}"
    payload = {
        "chatId": chat_id,
        "message": message,
    }
    with httpx.Client(timeout=20.0) as client:
        res = client.post(url, json=payload)
        res.raise_for_status()
        return res.json()


async def async_send_greenapi_message(phone: str, message: str, id_instance: str, token: str) -> dict:
    """
    Dedicated asynchronous Green-API message delivery function for async pipelines.
    """
    clean = re.sub(r"\D", "", phone)
    if len(clean) == 10 and settings.DEFAULT_COUNTRY_CODE:
        code = re.sub(r"\D", "", settings.DEFAULT_COUNTRY_CODE)
        clean = f"{code}{clean}"
    chat_id = f"{clean}@c.us"
    host = get_greenapi_host(id_instance)
    url = f"https://{host}/waInstance{id_instance}/sendMessage/{token}"
    payload = {
        "chatId": chat_id,
        "message": message,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        res = await client.post(url, json=payload)
        res.raise_for_status()
        return res.json()


def check_greenapi_whatsapp(phone: str, id_instance: str, token: str) -> dict:
    """
    Dedicated check if recipient WhatsApp account exists using Green-API checkWhatsapp endpoint:
    POST https://{host}/waInstance{idInstance}/checkWhatsapp/{token} with {"phoneNumber": clean_phone}
    """
    clean = re.sub(r"\D", "", phone)
    if len(clean) == 10 and settings.DEFAULT_COUNTRY_CODE:
        code = re.sub(r"\D", "", settings.DEFAULT_COUNTRY_CODE)
        clean = f"{code}{clean}"
    host = get_greenapi_host(id_instance)
    url = f"https://{host}/waInstance{id_instance}/checkWhatsapp/{token}"
    phone_val = int(clean) if clean.isdigit() else clean
    payload = {"phoneNumber": phone_val}

    with httpx.Client(timeout=15.0) as client:
        try:
            res = client.post(url, json=payload)
            if res.status_code == 200:
                return res.json()
        except Exception as e:
            logger.warning(f"[GREEN-API] sync checkWhatsapp POST error: {e}")

        try:
            res = client.get(url, params={"phoneNumber": str(phone_val)})
            if res.status_code == 200:
                return res.json()
        except Exception:
            pass

    return {"existsWhatsapp": True}


async def async_check_whatsapp_exists(phone: str, id_instance: str, token: str) -> dict:
    """
    Asynchronous check if recipient WhatsApp account exists using Green-API checkWhatsapp endpoint:
    POST https://{host}/waInstance{idInstance}/checkWhatsapp/{token} with {"phoneNumber": clean_phone}
    Returns e.g. {"existsWhatsapp": true/false, "chatId": "...", ...}
    """
    clean = re.sub(r"\D", "", phone)
    if len(clean) == 10 and settings.DEFAULT_COUNTRY_CODE:
        code = re.sub(r"\D", "", settings.DEFAULT_COUNTRY_CODE)
        clean = f"{code}{clean}"
    host = get_greenapi_host(id_instance)
    url = f"https://{host}/waInstance{id_instance}/checkWhatsapp/{token}"
    phone_val = int(clean) if clean.isdigit() else clean
    payload = {"phoneNumber": phone_val}

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            res = await client.post(url, json=payload)
            if res.status_code == 200:
                return res.json()
        except Exception as e:
            logger.warning(f"[GREEN-API] async checkWhatsapp POST failed: {e}")

        try:
            res = await client.get(url, params={"phoneNumber": str(phone_val)})
            if res.status_code == 200:
                return res.json()
        except Exception:
            pass

    return {"existsWhatsapp": True}


class GreenApiWhatsAppGateway(BaseWhatsAppGateway):
    """
    Green-API WhatsApp Gateway (Real WhatsApp Cloud Delivery via Linked Phone).
    Delivers messages directly to recipients' phones and syncs with sender's WhatsApp chat history.
    """

    def __init__(
        self,
        id_instance: Optional[str] = None,
        token: Optional[str] = None,
    ):
        self.default_id_instance = (id_instance or settings.GREENAPI_ID_INSTANCE or "710722761375").strip()
        self.default_token = (token or settings.GREENAPI_TOKEN or "d8dd5c9670e143359a359e3c83daa38b167180dbe7314e2aa4").strip()

    def clean_recipient_phone(self, phone: str) -> str:
        digits = re.sub(r"\D", "", phone)
        if len(digits) == 10 and settings.DEFAULT_COUNTRY_CODE:
            code = re.sub(r"\D", "", settings.DEFAULT_COUNTRY_CODE)
            digits = f"{code}{digits}"
        return digits

    async def check_recipient_whatsapp(
        self,
        to_phone: str,
        phone_number_id: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> dict:
        """Verify if a recipient phone is registered on WhatsApp."""
        id_inst = (phone_number_id or self.default_id_instance or "").strip()
        tok = (access_token or self.default_token or "").strip()
        clean = self.clean_recipient_phone(to_phone)
        return await async_check_whatsapp_exists(clean, id_inst, tok)

    async def send_text_message(
        self,
        to_phone: str,
        message: str,
        phone_number_id: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> WhatsAppSendResult:
        id_inst = (phone_number_id or self.default_id_instance or "").strip()
        tok = (access_token or self.default_token or "").strip()

        if not id_inst or not tok:
            err = "Missing Green-API credentials (idInstance or apiTokenInstance)."
            logger.error(f"[GREEN-API] {err}")
            return WhatsAppSendResult(success=False, error_message=err)

        clean = self.clean_recipient_phone(to_phone)
        if not clean or len(clean) < 7:
            err = f"Invalid recipient phone number format: '{to_phone}'"
            return WhatsAppSendResult(success=False, error_message=err)

        # Pre-flight check: Verify if recipient WhatsApp account exists using Green-API checkWhatsapp
        try:
            check_data = await async_check_whatsapp_exists(clean, id_inst, tok)
            if check_data.get("existsWhatsapp") is False:
                warn_msg = "Phone number is not registered on WhatsApp."
                logger.warning(f"[GREEN-API] Pre-flight check failed: Recipient {clean} is not registered on WhatsApp.")
                return WhatsAppSendResult(
                    success=False,
                    error_message=warn_msg,
                    raw_response=check_data,
                )
        except Exception as exc:
            logger.warning(f"[GREEN-API] checkWhatsapp check exception for {clean}: {exc}. Continuing with send attempt.")

        try:
            data = await async_send_greenapi_message(
                phone=clean,
                message=message,
                id_instance=id_inst,
                token=tok,
            )
            id_message = data.get("idMessage")
            if id_message:
                logger.info(f"[GREEN-API] Real message delivered to {clean} (idMessage: {id_message})")
                return WhatsAppSendResult(
                    success=True,
                    message_id=id_message,
                    raw_response=data,
                )
            else:
                err = f"Green-API response did not return idMessage: {data}"
                logger.error(f"[GREEN-API] {err}")
                return WhatsAppSendResult(success=False, error_message=err, raw_response=data)

        except httpx.HTTPStatusError as hse:
            err_body = hse.response.text
            if hse.response.status_code == 466 or "CORRESPONDENTS_QUOTE_EXCEEDED" in err_body or "quota has been exceeded" in err_body:
                from app.services.whatsapp_web_session import is_session_connected
                if is_session_connected():
                    logger.info(f"[GREEN-API] Quota exceeded on Green-API, but active WhatsApp Web session verified. Failing over to WhatsAppWebGateway for {clean}...")
                    web_gw = WhatsAppWebGateway()
                    return await web_gw.send_text_message(to_phone, message)

                err = (
                    f"Green-API Quota Exceeded (HTTP 466): Your Green-API instance is on the free Developer tariff which allows a maximum of 3 unique recipient contacts per month. "
                    f"Dr. Aayush ({clean}) is contact #4 and exceeds the 3-contact quota limit. To message Dr. Aayush and unlimited doctors, upgrade the tariff from 'Developer' to 'Business' / 'Basic' in your Green-API Console (https://console.green-api.com), or switch to WhatsApp Web QR Gateway."
                )
            else:
                err = f"Green-API HTTP {hse.response.status_code} Error: {err_body}"
            logger.error(f"[GREEN-API] {err}")
            return WhatsAppSendResult(
                success=False,
                error_message=err,
                raw_response={"status_code": hse.response.status_code, "body": err_body},
            )
        except Exception as exc:
            err = f"Green-API delivery error: {str(exc)}"
            logger.error(f"[GREEN-API] {err}")
            return WhatsAppSendResult(success=False, error_message=err)

    async def send_interactive_buttons(
        self,
        to_phone: str,
        body_text: str,
        buttons_list: Optional[list] = None,
        phone_number_id: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> WhatsAppSendResult:
        buttons = buttons_list or [
            {"type": "reply", "reply": {"id": "btn_confirm", "title": "Confirm"}},
            {"type": "reply", "reply": {"id": "btn_reschedule", "title": "Reschedule"}},
        ]
        btn_prompts = []
        for idx, btn in enumerate(buttons, 1):
            title = btn.get("reply", {}).get("title") or f"Option {idx}"
            btn_prompts.append(f"{idx}. {title}")

        formatted = f"{body_text}\n\n" + "\n".join(btn_prompts)
        return await self.send_text_message(to_phone, formatted, phone_number_id, access_token)

    async def test_connection(
        self,
        phone_number_id: str = "",
        access_token: str = "",
    ) -> dict:
        id_inst = (phone_number_id or self.default_id_instance or "").strip()
        tok = (access_token or self.default_token or "").strip()

        if not id_inst or not tok:
            return {"success": False, "message": "idInstance and apiTokenInstance must both be provided."}

        host = get_greenapi_host(id_inst)
        state_url = f"https://{host}/waInstance{id_inst}/getStateInstance/{tok}"
        settings_url = f"https://{host}/waInstance{id_inst}/getWaSettings/{tok}"

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                res = await client.get(state_url)
                if res.status_code == 200:
                    state_data = res.json()
                    st = state_data.get("stateInstance")
                    if st == "authorized":
                        set_res = await client.get(settings_url)
                        phone = "Linked WhatsApp Phone"
                        if set_res.status_code == 200:
                            s_data = set_res.json()
                            phone = s_data.get("phone") or phone
                        return {
                            "success": True,
                            "state": "authorized",
                            "phone": phone,
                            "message": f"Green-API Connected! Authorized as +{phone}",
                            "details": state_data,
                        }
                    else:
                        return {
                            "success": False,
                            "state": st,
                            "message": f"Green-API state is '{st}'. Please link your WhatsApp in the Green-API console.",
                            "details": state_data,
                        }
                else:
                    return {
                        "success": False,
                        "message": f"Green-API returned HTTP {res.status_code}: {res.text}",
                    }
        except Exception as exc:
            return {"success": False, "message": f"Green-API connection error: {str(exc)}"}


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
            from app.services.whatsapp_web_session import is_session_connected
            if is_session_connected():
                logger.info("Meta credentials missing, but WhatsApp Web session is actively connected. Rerouting to WhatsAppWebGateway.")
                web_gw = WhatsAppWebGateway()
                return await web_gw.send_text_message(to_phone, message)

            err = (
                "Missing Meta WhatsApp Cloud API credentials. Please set Phone Number ID "
                "and WhatsApp Access Token in Settings, or link your WhatsApp Web QR session."
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
            from app.services.whatsapp_web_session import is_session_connected
            if is_session_connected():
                logger.info("Meta credentials missing, but WhatsApp Web session is actively connected. Rerouting interactive dispatch to WhatsAppWebGateway.")
                web_gw = WhatsAppWebGateway()
                return await web_gw.send_interactive_buttons(to_phone, body_text, buttons_list)

            err = (
                "Missing Meta WhatsApp Cloud API credentials. Please set Phone Number ID "
                "and WhatsApp Access Token in Settings, or link your WhatsApp Web QR session."
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


class WhatsAppWebGateway(BaseWhatsAppGateway):
    """
    WhatsApp Web QR-Code Gateway (powered by Node.js Baileys Bridge).
    Connects to personal or business WhatsApp via QR code scan without requiring Meta tokens.
    """

    def __init__(self, bridge_url: Optional[str] = None):
        self.bridge_url = (bridge_url or settings.WA_BRIDGE_URL or "http://127.0.0.1:3001").rstrip("/")

    def clean_recipient_phone(self, phone: str) -> str:
        digits = re.sub(r"\D", "", phone)
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
        clean_phone = self.clean_recipient_phone(to_phone)

        # Genuine dispatch via Node.js Baileys Multi-Device WebSocket bridge
        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                res = await client.post(
                    f"{self.bridge_url}/send",
                    json={"to": clean_phone, "message": message},
                )
                data = res.json()
                if res.status_code == 200 and data.get("success"):
                    msg_id = data.get("message_id")
                    logger.info(f"[WHATSAPP WEB] Genuine dispatch to {clean_phone} via Baileys socket (Msg ID: {msg_id})")
                    return WhatsAppSendResult(
                        success=True,
                        message_id=msg_id,
                        raw_response=data,
                    )
                else:
                    err_msg = data.get("error") or f"Bridge HTTP {res.status_code} Error: {res.text}"
                    logger.error(f"[WHATSAPP WEB] Baileys dispatch failed to {clean_phone}: {err_msg}")
                    return WhatsAppSendResult(
                        success=False,
                        error_message=err_msg,
                        raw_response=data,
                    )
        except httpx.ConnectError:
            err = (
                f"WhatsApp Web Baileys bridge is unreachable at {self.bridge_url}. "
                "The background WebSocket process is not running. Please start the bridge and scan the QR code."
            )
            logger.error(f"[WHATSAPP WEB] {err}")
            return WhatsAppSendResult(success=False, error_message=err)
        except Exception as exc:
            err = f"WhatsApp Web bridge communication error: {str(exc)}"
            logger.error(f"[WHATSAPP WEB] {err}")
            return WhatsAppSendResult(success=False, error_message=err)

    async def send_interactive_buttons(
        self,
        to_phone: str,
        body_text: str,
        buttons_list: Optional[list] = None,
        phone_number_id: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> WhatsAppSendResult:
        buttons = buttons_list or [
            {"type": "reply", "reply": {"id": "btn_confirm", "title": "✅ Confirm"}},
            {"type": "reply", "reply": {"id": "btn_reschedule", "title": "📅 Reschedule"}},
        ]
        btn_prompts = []
        for idx, btn in enumerate(buttons, 1):
            title = btn.get("reply", {}).get("title") or f"Option {idx}"
            btn_prompts.append(f"{idx}️⃣ {title}")

        formatted_msg = f"{body_text}\n\n" + "\n".join(btn_prompts)
        return await self.send_text_message(to_phone, formatted_msg)

    async def test_connection(
        self,
        phone_number_id: str = "",
        access_token: str = "",
    ) -> dict:
        # Check Node Baileys bridge
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                res = await client.get(f"{self.bridge_url}/status")
                if res.status_code == 200:
                    data = res.json()
                    st = data.get("status")
                    if st == "connected":
                        user = data.get("user") or {}
                        phone = user.get("phone", "Linked Device")
                        return {"success": True, "message": f"WhatsApp Web Baileys active! Connected as {phone}"}
                    elif st == "qr_ready":
                        return {"success": False, "message": "WhatsApp Web QR code ready. Please scan from Settings tab."}
                    else:
                        return {"success": False, "message": f"WhatsApp Web status is '{st}'. Scan the QR code in Settings to connect."}
        except httpx.ConnectError:
            return {
                "success": False,
                "message": f"Cannot connect to Baileys bridge on {self.bridge_url}. The background WebSocket bridge process is offline.",
            }
        except Exception as exc:
            return {"success": False, "message": f"Bridge error: {str(exc)}"}

        return {
            "success": False,
            "message": "WhatsApp Web is not connected. Click 'Generate WhatsApp Login QR' in Settings.",
        }


def get_whatsapp_gateway(gateway_type: Optional[str] = None) -> BaseWhatsAppGateway:
    """Factory to create configured gateway with resilient fallback."""
    from app.services.whatsapp_web_session import is_session_connected

    mode = (gateway_type or settings.WHATSAPP_GATEWAY_TYPE or "greenapi").strip().lower()

    # 1. Green-API (Real WhatsApp Delivery via Linked Phone)
    if mode in ("greenapi", "green_api", "green", "green-api"):
        return GreenApiWhatsAppGateway()

    # 2. WhatsApp Web QR Gateway aliases
    elif mode in ("whatsapp_web_qr", "web", "whatsapp_web", "baileys", "qr", "web_qr", "qr_gateway"):
        return WhatsAppWebGateway()

    # 3. Simulated Mock Gateway
    elif mode in ("mock", "simulated", "dry_run"):
        return MockWhatsAppGateway()

    # 4. Meta Cloud API Gateway
    elif mode in ("meta", "meta_cloud_api", "cloud_api"):
        return MetaCloudWhatsAppGateway()

    # 5. Fallback: default to Green-API
    return GreenApiWhatsAppGateway()


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
