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


def evaluate_reply_intent(text: str) -> str:
    """
    Evaluate customer WhatsApp reply body and determine new reminder status:
    - '1', 'yes', 'confirm', 'confirmed', 'y', 'sure' -> CONFIRMED
    - 'cancel', 'cannot', 'cant', 'no' -> CANCELLED
    - '2', 'reschedule', 'later', 'change', 'postpone' -> NEEDS_RESCHEDULE
    """
    clean = text.lower().strip()

    # Priority 1: Cancellation check
    if any(k in clean for k in ["cancel", "cannot make", "cant make", "won't make", "no", "unable"]):
        return "CANCELLED"

    # Priority 2: Reschedule check
    if clean == "2" or any(k in clean for k in ["reschedule", "change time", "change date", "postpone", "later", "next week"]):
        return "NEEDS_RESCHEDULE"

    # Priority 3: Confirmation check
    if clean == "1" or any(k in clean for k in ["yes", "confirm", "confirmed", "yup", "yeah", "ok", "sure", "attend", "i will be there"]):
        return "CONFIRMED"

    # Default fallback: If ambiguous, keep as NEEDS_RESCHEDULE so clinic staff can follow up
    return "NEEDS_RESCHEDULE"


async def process_incoming_reply(db: AsyncSession, from_phone: str, text_body: str) -> Optional[Reminder]:
    """
    Match incoming sender phone to the most recent Reminder record,
    update its status, and record the reply text and timestamp.
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
        new_status = evaluate_reply_intent(text_body)
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
            payload=json.dumps({"from": from_phone, "text": text_body, "matched_status": new_status}),
        )
        db.add(audit)
        await db.commit()
        await db.refresh(reminder)

        logger.info(
            f"Updated reminder [{reminder.id}] for {reminder.customer_name} ({reminder.phone}) "
            f"to {new_status} via customer reply: '{text_body}'"
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

            # B. Process incoming customer replies
            messages = value.get("messages", [])
            for msg in messages:
                from_phone = msg.get("from")
                msg_type = msg.get("type")
                text_body = msg.get("text", {}).get("body", "") if msg_type == "text" else ""

                if from_phone and text_body:
                    await process_incoming_reply(db, from_phone=from_phone, text_body=text_body)

    await db.commit()
    return Response(content="EVENT_RECEIVED", status_code=status.HTTP_200_OK)


# 3. Simulate Incoming Reply Endpoint for UI Playground and Demos
class SimulateReplyPayload(BaseModel):
    phone: str = Field(..., description="Customer phone number to simulate reply from")
    message: str = Field(..., description="Reply text (e.g., '1', 'YES', '2', 'Reschedule')")


@router.post("/api/webhook/simulate", summary="Simulate Inbound WhatsApp Reply (Demo & Testing)")
async def simulate_incoming_reply(payload: SimulateReplyPayload, db: AsyncSession = Depends(get_db)):
    """
    Allows clinic owners and developers to test the two-way interaction engine directly from the UI
    without needing an external ngrok tunnel or live Meta webhook setup.
    """
    reminder = await process_incoming_reply(db, from_phone=payload.phone, text_body=payload.message)
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
        "replied_at": reminder.replied_at.isoformat() if reminder.replied_at else None,
    }
