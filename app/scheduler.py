"""
Automated Dispatch Loop using APScheduler AsyncIOScheduler.
Polls for due appointments, coordinates Gemini AI message personalization,
enforces anti-ban randomized jitter throttling, and dispatches via WhatsApp.
"""
import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.config import settings
from app.database import (
    AsyncSessionLocal,
    Client,
    MessageAuditLog,
    Reminder,
    get_all_settings_dict,
)
from app.services.gemini_service import gemini_service
from app.services.whatsapp_service import get_whatsapp_gateway

logger = logging.getLogger("whatsapp_ai.scheduler")

scheduler: Optional[AsyncIOScheduler] = None
_dispatch_lock = asyncio.Lock()


async def dispatch_due_reminders_task() -> None:
    """
    Periodic job executed every minute.
    Pulls PENDING reminders due for delivery, generates AI text, dispatches via WhatsApp,
    and applies randomized jitter sleep between sends to prevent anti-spam triggers.
    """
    if _dispatch_lock.locked():
        logger.info("Previous dispatch cycle still in progress. Skipping tick to prevent overlap.")
        return

    async with _dispatch_lock:
        now_utc = datetime.now(timezone.utc)
        logger.debug(f"Starting reminder scan at {now_utc.isoformat()}...")

        async with AsyncSessionLocal() as session:
            try:
                # 1. Fetch stored dynamic settings
                stored_settings = await get_all_settings_dict(session)
                default_biz_name = stored_settings.get("business_name") or settings.DEFAULT_BUSINESS_NAME
                default_biz_type = stored_settings.get("business_type") or settings.DEFAULT_BUSINESS_TYPE
                default_tone = stored_settings.get("default_tone") or "friendly"
                global_phone_id = stored_settings.get("meta_phone_number_id") or settings.META_PHONE_NUMBER_ID
                global_token = stored_settings.get("meta_whatsapp_token") or settings.META_WHATSAPP_TOKEN
                global_gemini_key = stored_settings.get("gemini_api_key") or settings.GEMINI_API_KEY
                global_gemini_model = stored_settings.get("gemini_model") or settings.GEMINI_MODEL
                active_gateway_type = stored_settings.get("whatsapp_gateway_type") or settings.WHATSAPP_GATEWAY_TYPE

                # 2. Fetch due reminders up to MAX_BATCH_SIZE
                stmt = (
                    select(Reminder)
                    .where(
                        Reminder.status == "PENDING",
                        Reminder.reminder_time <= now_utc,
                    )
                    .order_by(Reminder.reminder_time.asc())
                    .limit(settings.MAX_BATCH_SIZE)
                )
                result = await session.execute(stmt)
                due_reminders = result.scalars().all()

                if not due_reminders:
                    logger.debug("No due reminders found in this cycle.")
                    return

                logger.info(f"Found {len(due_reminders)} due reminder(s) to process.")

                # 3. Mark batch as PROCESSING immediately to prevent concurrent worker picking
                for r in due_reminders:
                    r.status = "PROCESSING"
                await session.commit()

                # 4. Process each reminder sequentially with anti-ban rate limiting jitter
                total_in_batch = len(due_reminders)
                active_gateway = get_whatsapp_gateway(active_gateway_type)

                for index, reminder in enumerate(due_reminders):
                    # Fetch client profile for tenant-specific business details
                    client_stmt = select(Client).where(Client.id == reminder.client_id)
                    client_res = await session.execute(client_stmt)
                    client = client_res.scalar_one_or_none()

                    business_name = (client.name if client else None) or default_biz_name
                    business_type = (client.business_type if client else None) or default_biz_type
                    active_tone = reminder.tone or (client.default_tone if client else None) or default_tone

                    phone_id = (client.phone_number_id if client else None) or global_phone_id
                    access_token = (client.whatsapp_token if client else None) or global_token

                    logger.info(
                        f"Processing reminder [{reminder.id}] for {reminder.customer_name} "
                        f"({reminder.phone}) at {business_name}"
                    )

                    # A. Personalize message via Gemini AI (or fallback template)
                    custom_text = await gemini_service.generate_reminder(
                        customer_name=reminder.customer_name,
                        appointment_datetime=reminder.appointment_datetime,
                        service_name=reminder.service_name,
                        notes=reminder.notes,
                        tone=active_tone,
                        business_name=business_name,
                        business_type=business_type,
                        custom_api_key=global_gemini_key,
                        model_name=global_gemini_model,
                    )

                    # B. Dispatch via configured WhatsApp gateway with interactive quick-reply buttons
                    send_result = await active_gateway.send_interactive_buttons(
                        to_phone=reminder.phone,
                        body_text=custom_text,
                        phone_number_id=phone_id,
                        access_token=access_token,
                    )

                    # C. Update database record with result
                    reminder.attempts += 1
                    reminder.generated_message = custom_text

                    if send_result.success:
                        reminder.status = "SENT"
                        reminder.whatsapp_message_id = send_result.message_id
                        reminder.sent_at = datetime.now(timezone.utc)
                        reminder.error_log = None
                        logger.info(f"Reminder [{reminder.id}] sent successfully via {active_gateway_type}.")
                    else:
                        reminder.error_log = send_result.error_message
                        if reminder.attempts >= reminder.max_retries:
                            reminder.status = "FAILED"
                            logger.error(
                                f"Reminder [{reminder.id}] permanently failed after {reminder.attempts} attempts: "
                                f"{send_result.error_message}"
                            )
                        else:
                            # Requeue for retry after 5 minutes
                            reminder.status = "PENDING"
                            logger.warning(
                                f"Reminder [{reminder.id}] delivery failed (attempt {reminder.attempts}/{reminder.max_retries}): "
                                f"{send_result.error_message}. Requeuing."
                            )

                    # D. Record audit trail
                    audit = MessageAuditLog(
                        client_id=reminder.client_id,
                        reminder_id=reminder.id,
                        direction="OUTBOUND",
                        status=reminder.status,
                        payload=custom_text,
                    )
                    session.add(audit)
                    await session.commit()

                    # E. Apply humanized randomized jitter delay between sends (Anti-Ban feature)
                    if index < total_in_batch - 1:
                        jitter = random.uniform(
                            settings.RATE_LIMIT_MIN_JITTER_SECONDS,
                            settings.RATE_LIMIT_MAX_JITTER_SECONDS,
                        )
                        logger.debug(f"Throttling next dispatch by {jitter:.2f}s jitter...")
                        await asyncio.sleep(jitter)

                logger.info(f"Finished dispatch run for {total_in_batch} reminder(s).")

            except Exception as exc:
                logger.error(f"Unexpected error in dispatch_due_reminders_task: {exc}", exc_info=True)
                await session.rollback()


def start_scheduler() -> AsyncIOScheduler:
    """Start APScheduler in the background loop."""
    global scheduler
    if scheduler is not None and scheduler.running:
        logger.warning("APScheduler already active.")
        return scheduler

    scheduler = AsyncIOScheduler(timezone=timezone.utc)
    scheduler.add_job(
        dispatch_due_reminders_task,
        trigger=IntervalTrigger(seconds=settings.SCHEDULER_INTERVAL_SECONDS),
        id="whatsapp_reminder_dispatcher",
        name="Poll & Dispatch Due Reminders",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info(
        f"APScheduler worker started. Scanning for due reminders every {settings.SCHEDULER_INTERVAL_SECONDS} seconds."
    )
    return scheduler


def stop_scheduler() -> None:
    """Gracefully terminate APScheduler."""
    global scheduler
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler worker terminated gracefully.")
        scheduler = None


def is_scheduler_running() -> bool:
    """Check if scheduler instance is actively running."""
    return bool(scheduler and scheduler.running)
