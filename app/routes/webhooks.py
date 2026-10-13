"""
Two-Way WhatsApp Webhook Listener:
Handles Meta Graph API Webhook verification handshake (GET /api/webhook & GET /webhook)
and real-time inbound customer reply tracking (POST /api/webhook & POST /webhook).
Updates reminder status to CONFIRMED, NEEDS_RESCHEDULE, or CANCELLED based on customer replies.
"""
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import MessageAuditLog, Reminder, get_db

logger = logging.getLogger("whatsapp_ai.routes.webhooks")
router = APIRouter(tags=["WhatsApp Webhook"])


def clean_phone_digits(phone: str) -> str:
    """Normalize phone number to digits only for reliable lookup."""
    return re.sub(r"\D", "", phone)


def evaluate_reply_intent(text: str, button_id: Optional[str] = None) -> str:
    """
    Evaluate customer WhatsApp reply body or interactive button ID and determine new reminder status:
    - button_id == 'btn_confirm' or text contains '✅ confirm', '1', 'confirm' -> CONFIRMED
    - button_id == 'btn_reschedule' or text contains '📅 reschedule', '2', 'reschedule' -> NEEDS_RESCHEDULE
    - text contains 'cancel', 'cannot', 'no' -> CANCELLED
    """
    b_id = (button_id or "").lower().strip()
    if b_id == "btn_confirm" or "confirm" in b_id:
        return "CONFIRMED"
    if b_id == "btn_reschedule" or "reschedule" in b_id:
        return "NEEDS_RESCHEDULE"

    clean = (text or "").lower().strip()

    # Priority 1: Interactive quick-reply titles
    if "confirm" in clean or "✅" in clean or clean == "1":
        return "CONFIRMED"
    if "reschedule" in clean or "📅" in clean or clean == "2":
        return "NEEDS_RESCHEDULE"

    # Priority 2: Explicit cancellation check
    if any(k in clean for k in ["cancel", "cannot make", "cant make", "won't make", "no", "unable"]):
        return "CANCELLED"

    # Priority 3: Phrasing check
    if any(k in clean for k in ["change time", "change date", "postpone", "later", "next week"]):
        return "NEEDS_RESCHEDULE"
    if any(k in clean for k in ["yes", "confirmed", "yup", "yeah", "ok", "sure", "attend", "i will be there"]):
        return "CONFIRMED"

    # Default fallback: If ambiguous, keep as NEEDS_RESCHEDULE so clinic staff can follow up
    return "NEEDS_RESCHEDULE"


async def process_incoming_reply(
    db: AsyncSession,
    from_phone: str,
    text_body: str,
    button_id: Optional[str] = None,
) -> Optional[Reminder]:
    """
    Match incoming sender phone to the most recent Reminder record,
    update its status, and record the reply text and timestamp.
    Supports both text replies and Meta interactive button replies.
    """
    digits = clean_phone_digits(from_phone)
    if not digits:
        return None

    # Query reminder matching digits (check both sanitized and raw)
    stmt = (
        select(Reminder)
        .where(
            or_(
                Reminder.phone.ilike(f"%{digits[-10:]}%"),
                Reminder.phone == f"+{digits}",
                Reminder.phone == digits,
            )
        )
        .order_by(Reminder.appointment_datetime.desc(), Reminder.created_at.desc())
    )
    res = await db.execute(stmt)
    reminder = res.scalars().first()

    if reminder:
        new_status = evaluate_reply_intent(text_body, button_id=button_id)
        reminder.status = new_status
        reminder.customer_reply = text_body
        reminder.replied_at = datetime.now(timezone.utc)

        # Audit log
        audit = MessageAuditLog(
            user_id=reminder.user_id,
            client_id=reminder.client_id,
            reminder_id=reminder.id,
            direction="INBOUND",
            status=new_status,
            payload=json.dumps({
                "from": from_phone,
                "text": text_body,
                "button_id": button_id,
                "matched_status": new_status,
            }),
        )
        db.add(audit)
        await db.commit()
        await db.refresh(reminder)

        logger.info(
            f"Updated reminder [{reminder.id}] for {reminder.customer_name} ({reminder.phone}) "
            f"to {new_status} via customer reply: '{text_body}' (button: {button_id})"
        )
        return reminder

    logger.warning(f"No active reminder found matching incoming phone digits: '{digits}'")
    return None


# 1. GET Handlers for Meta Webhook Verification (supporting /api/webhook and /webhook)
@router.get("/api/webhook", summary="Meta Webhook Verification Handshake (/api/webhook)")
@router.get("/webhook", summary="Meta Webhook Verification Handshake (/webhook)")
async def verify_webhook_handshake(
    hub_mode: str = Query(..., alias="hub.mode"),
    hub_challenge: str = Query(..., alias="hub.challenge"),
    hub_verify_token: str = Query(..., alias="hub.verify_token"),
):
    """
    Responds to Meta's Webhook callback URL verification challenge.
    Verifies that hub.mode == 'subscribe' and hub.verify_token matches configured secret.
    """
    expected_token = settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN or settings.META_VERIFY_TOKEN

    if hub_mode == "subscribe" and hub_verify_token == expected_token:
        logger.info("Meta Webhook verification handshake succeeded.")
        return Response(content=hub_challenge, media_type="text/plain", status_code=status.HTTP_200_OK)

    logger.warning(f"Meta Webhook verification token mismatch: got '{hub_verify_token}' vs expected '{expected_token}'")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Verification token mismatch.")


@router.get("/api/webhook/test-ping", summary="Check Webhook Reachability & Configuration")
async def ping_webhook():
    """
    Test endpoint for UI dashboard to verify that the webhook listener is active,
    online, and configured with the expected verify token.
    """
    verify_token = settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN or settings.META_VERIFY_TOKEN
    return {
        "status": "online",
        "reachable": True,
        "endpoint": "/api/webhook",
        "verify_token": verify_token,
        "supported_methods": ["GET", "POST"],
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


# 2. POST Handlers for Inbound WhatsApp Messages and Status Receipts
@router.post("/api/webhook", summary="Meta Webhook Inbound Event Callback (/api/webhook)")
@router.post("/webhook", summary="Meta Webhook Inbound Event Callback (/webhook)")
async def receive_webhook_event(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Receives real-time asynchronous callbacks from WhatsApp Cloud API:
    - Message delivery receipts ('sent', 'delivered', 'read', 'failed')
    - Inbound customer text replies ('1', '2', 'YES', 'Reschedule')
    """
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        return Response(content="Invalid JSON", status_code=status.HTTP_400_BAD_REQUEST)

    # Process standard Meta Graph API webhook structure
    entries = payload.get("entry", [])
    for entry in entries:
        changes = entry.get("changes", [])
        for change in changes:
            value = change.get("value", {})

            # A. Process delivery status receipts
            statuses = value.get("statuses", [])
            for st in statuses:
                msg_id = st.get("id")
                status_name = st.get("status")
                if msg_id and status_name:
                    rem_res = await db.execute(select(Reminder).where(Reminder.whatsapp_message_id == msg_id))
                    reminder = rem_res.scalar_one_or_none()
                    if reminder:
                        if status_name == "failed":
                            reminder.status = "FAILED"
                            errors = st.get("errors", [{}])
                            err_title = errors[0].get("title", "Delivery failure") if errors else "Failed"
                            reminder.error_log = f"Meta delivery error: {err_title}"
                        elif status_name in ("delivered", "read") and reminder.status == "PENDING":
                            reminder.status = "SENT"

            # B. Process incoming customer replies (Text or Interactive Quick Replies)
            messages = value.get("messages", [])
            for msg in messages:
                from_phone = msg.get("from")
                msg_type = msg.get("type")
                text_body = ""
                button_id = None

                if msg_type == "text":
                    text_body = msg.get("text", {}).get("body", "")
                elif msg_type == "interactive":
                    interactive = msg.get("interactive", {})
                    btn_reply = interactive.get("button_reply", {})
                    button_id = btn_reply.get("id")
                    text_body = btn_reply.get("title", "")
                elif msg_type == "button":
                    btn = msg.get("button", {})
                    button_id = btn.get("payload")
                    text_body = btn.get("text", "")

                if from_phone and (text_body or button_id):
                    if not text_body:
                        text_body = "✅ Confirm" if button_id == "btn_confirm" else "📅 Reschedule"
                    await process_incoming_reply(
                        db,
                        from_phone=from_phone,
                        text_body=text_body,
                        button_id=button_id,
                    )

    await db.commit()
    return Response(content="EVENT_RECEIVED", status_code=status.HTTP_200_OK)


# 3. Simulate Incoming Reply Endpoint for UI Playground and Demos
class SimulateReplyPayload(BaseModel):
    phone: str = Field(..., description="Customer phone number to simulate reply from")
    message: Optional[str] = Field(None, description="Reply text (e.g., '1', 'YES', '2', 'Reschedule', '✅ Confirm', '📅 Reschedule')")
    button_id: Optional[str] = Field(None, description="Interactive quick-reply button ID ('btn_confirm' or 'btn_reschedule')")


@router.post("/api/webhook/simulate", summary="Simulate Inbound WhatsApp Reply (Demo & Testing)")
async def simulate_incoming_reply(payload: SimulateReplyPayload, db: AsyncSession = Depends(get_db)):
    """
    Allows clinic owners and developers to test the two-way interaction engine directly from the UI
    without needing an external ngrok tunnel or live Meta webhook setup.
    Supports both plain text replies and Meta interactive quick-reply buttons ('btn_confirm' / 'btn_reschedule').
    """
    reply_text = payload.message
    if not reply_text:
        if payload.button_id == "btn_confirm":
            reply_text = "✅ Confirm"
        elif payload.button_id == "btn_reschedule":
            reply_text = "📅 Reschedule"
        else:
            reply_text = "1 - Confirmed"

    reminder = await process_incoming_reply(
        db,
        from_phone=payload.phone,
        text_body=reply_text,
        button_id=payload.button_id,
    )
    if not reminder:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No scheduled reminder found for phone number '{payload.phone}'.",
        )

    return {
        "success": True,
        "reminder_id": reminder.id,
        "customer_name": reminder.customer_name,
        "new_status": reminder.status,
        "customer_reply": reminder.customer_reply,
        "button_id": payload.button_id,
        "replied_at": reminder.replied_at.isoformat() if reminder.replied_at else None,
    }
