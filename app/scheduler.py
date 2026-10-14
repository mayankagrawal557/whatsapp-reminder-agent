"""
Automated Doctor Engagement & Broadcast Scheduler for Arovida PET-CT Centre.
Coordinates flexible recurring broadcasts (every Monday, 1st & 16th, daily, 1-click blasts),
applies Meta Cloud API rate-limiting delays (0.2s–0.5s pause), and logs dispatch results.
"""
import asyncio
import logging
import random
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.config import settings
from app.database import (
    AsyncSessionLocal,
    BroadcastCampaign,
    BroadcastLog,
    DoctorContact,
    User,
    get_user_settings_dict,
)
from app.services.whatsapp_service import get_whatsapp_gateway

logger = logging.getLogger("whatsapp_ai.scheduler")

scheduler: Optional[AsyncIOScheduler] = None
_broadcast_lock = asyncio.Lock()


def parse_scheduled_time(time_str: str) -> tuple[int, int]:
    """Parse 'HH:MM' string to (hour, minute) integers."""
    try:
        parts = (time_str or "10:00").strip().split(":")
        return int(parts[0]), int(parts[1])
    except Exception:
        return 10, 0


def calculate_next_run_time(
    cron_schedule: Optional[str],
    scheduled_time: str = "10:00",
    from_dt: Optional[datetime] = None,
) -> Optional[datetime]:
    """
    Calculate the next execution timestamp based on flexible recurring rules:
    - 'every_monday': Next upcoming Monday at scheduled_time (e.g. 10:00 AM)
    - 'every_friday': Next upcoming Friday at scheduled_time
    - 'dates_1_and_16': Next 1st or 16th day of month at scheduled_time
    - 'daily': Next occurrence tomorrow at scheduled_time
    - 'immediate': Current timestamp
    """
    base_dt = from_dt or datetime.now(timezone.utc)
    hour, minute = parse_scheduled_time(scheduled_time)
    sched = (cron_schedule or "").strip().lower()

    if sched == "immediate":
        return base_dt

    if sched == "every_monday":
        # Monday is weekday 0
        candidate = base_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        days_ahead = (0 - base_dt.weekday()) % 7
        if days_ahead == 0 and candidate <= base_dt:
            days_ahead = 7
        return candidate + timedelta(days=days_ahead)

    elif sched == "every_friday":
        # Friday is weekday 4
        candidate = base_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        days_ahead = (4 - base_dt.weekday()) % 7
        if days_ahead == 0 and candidate <= base_dt:
            days_ahead = 7
        return candidate + timedelta(days=days_ahead)

    elif sched == "dates_1_and_16":
        # 1st and 16th of every month
        year = base_dt.year
        month = base_dt.month
        candidate_1st = datetime(year, month, 1, hour, minute, 0, tzinfo=timezone.utc)
        candidate_16th = datetime(year, month, 16, hour, minute, 0, tzinfo=timezone.utc)

        if base_dt < candidate_1st:
            return candidate_1st
        elif base_dt < candidate_16th:
            return candidate_16th
        else:
            # Advance to 1st of next month
            next_month = month + 1 if month < 12 else 1
            next_year = year if month < 12 else year + 1
            return datetime(next_year, next_month, 1, hour, minute, 0, tzinfo=timezone.utc)

    elif sched == "daily":
        candidate = base_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= base_dt:
            candidate += timedelta(days=1)
        return candidate

    # Default fallback: tomorrow at specified time
    candidate = base_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= base_dt:
        candidate += timedelta(days=1)
    return candidate


async def execute_campaign_broadcast(campaign_id: str, session) -> Dict[str, Any]:
    """
    Execute a doctor broadcast campaign:
    1. Fetch campaign and verify active status.
    2. Retrieve all active referring doctors for that clinic tenant.
    3. Retrieve WhatsApp credentials (Meta Cloud API or Mock mode).
    4. Personalize message with dynamic tags: {doctor_name}, {specialty}, {hospital}, {centre_name}.
    5. Dispatch with rate-limiting pauses (0.2s - 0.4s) to safely send 500+ messages without Meta blocking.
    6. Record logs in BroadcastLog and update campaign stats.
    """
    stmt = select(BroadcastCampaign).where(BroadcastCampaign.id == campaign_id)
    res = await session.execute(stmt)
    campaign = res.scalar_one_or_none()

    if not campaign:
        logger.error(f"Campaign [{campaign_id}] not found.")
        return {"success": False, "error": "Campaign not found"}

    user_stmt = select(User).where(User.id == campaign.user_id)
    user_res = await session.execute(user_stmt)
    centre_user = user_res.scalar_one_or_none()

    centre_name = centre_user.business_name if centre_user else settings.DEFAULT_BUSINESS_NAME

    # Fetch active doctors for this centre
    doc_stmt = (
        select(DoctorContact)
        .where(
            DoctorContact.user_id == campaign.user_id,
            DoctorContact.is_active == True,
        )
        .order_by(DoctorContact.doctor_name.asc())
    )
    doc_res = await session.execute(doc_stmt)
    active_doctors = doc_res.scalars().all()

    total_doctors = len(active_doctors)
    logger.info(f"Executing Campaign [{campaign.campaign_title}] to {total_doctors} active doctors.")

    if total_doctors == 0:
        campaign.last_run_at = datetime.now(timezone.utc)
        campaign.total_recipients = 0
        if campaign.schedule_type == "ONE_TIME":
            campaign.status = "COMPLETED"
            campaign.next_run_at = None
        else:
            campaign.next_run_at = calculate_next_run_time(campaign.cron_schedule, campaign.scheduled_time)
        await session.commit()
        return {"success": True, "sent": 0, "failed": 0, "message": "No active doctors in directory"}

    # Fetch tenant WhatsApp settings
    tenant_settings = await get_user_settings_dict(session, campaign.user_id)
    active_gateway_type = tenant_settings.get("whatsapp_gateway_type") or settings.WHATSAPP_GATEWAY_TYPE
    phone_id = tenant_settings.get("meta_phone_number_id") or settings.META_PHONE_NUMBER_ID
    access_token = tenant_settings.get("meta_whatsapp_token") or settings.META_WHATSAPP_TOKEN
    gateway = get_whatsapp_gateway(active_gateway_type)

    sent_count = 0
    failed_count = 0
    now_utc = datetime.now(timezone.utc)

    for index, doctor in enumerate(active_doctors):
        # 1. Personalize template tags
        doc_specialty = doctor.specialty or "Doctor / Oncologist"
        doc_hospital = doctor.hospital or "Medical Practice"
        personalized_message = (
            campaign.template_message
            .replace("{doctor_name}", doctor.doctor_name)
            .replace("{specialty}", doc_specialty)
            .replace("{hospital}", doc_hospital)
            .replace("{centre_name}", centre_name)
        )

        # 2. Dispatch via WhatsApp Gateway
        try:
            dispatch_res = await gateway.send_text_message(
                to_phone=doctor.phone_number,
                message=personalized_message,
                phone_number_id=phone_id,
                access_token=access_token,
            )

            status_str = "SENT" if dispatch_res.success else "FAILED"
            error_str = dispatch_res.error_message if not dispatch_res.success else None
            msg_id = dispatch_res.message_id

            if dispatch_res.success:
                sent_count += 1
            else:
                failed_count += 1
        except Exception as exc:
            logger.error(f"Error dispatching to {doctor.doctor_name} ({doctor.phone_number}): {exc}")
            status_str = "FAILED"
            error_str = str(exc)
            msg_id = None
            failed_count += 1

        # 3. Create log entry
        log_entry = BroadcastLog(
            user_id=campaign.user_id,
            campaign_id=campaign.id,
            doctor_id=doctor.id,
            doctor_name=doctor.doctor_name,
            phone_number=doctor.phone_number,
            message_snippet=personalized_message[:160] + ("..." if len(personalized_message) > 160 else ""),
            sent_at=datetime.now(timezone.utc),
            status=status_str,
            error_message=error_str,
            whatsapp_message_id=msg_id,
        )
        session.add(log_entry)

        # 4. Respect Meta Cloud API rate limits (0.2s–0.4s pause)
        if index < total_doctors - 1:
            await asyncio.sleep(random.uniform(0.2, 0.4))

    # Update campaign statistics
    campaign.last_run_at = datetime.now(timezone.utc)
    campaign.total_recipients = total_doctors
    campaign.total_sent = sent_count
    campaign.total_failed = failed_count

    if campaign.schedule_type == "ONE_TIME":
        campaign.status = "COMPLETED"
        campaign.next_run_at = None
    else:
        campaign.next_run_at = calculate_next_run_time(campaign.cron_schedule, campaign.scheduled_time, from_dt=now_utc)

    await session.commit()
    logger.info(f"Campaign [{campaign.campaign_title}] completed: {sent_count} sent, {failed_count} failed.")

    return {
        "success": True,
        "campaign_id": campaign.id,
        "total": total_doctors,
        "sent": sent_count,
        "failed": failed_count,
    }


async def check_and_dispatch_scheduled_campaigns() -> None:
    """
    APScheduler periodic job: scans every 30 seconds for campaigns
    whose status is ACTIVE and next_run_at is due (<= now).
    """
    if _broadcast_lock.locked():
        logger.debug("Broadcast scan already running. Skipping tick.")
        return

    async with _broadcast_lock:
        now_utc = datetime.now(timezone.utc)

        async with AsyncSessionLocal() as session:
            try:
                stmt = (
                    select(BroadcastCampaign)
                    .where(
                        BroadcastCampaign.status == "ACTIVE",
                        BroadcastCampaign.next_run_at <= now_utc,
                    )
                    .order_by(BroadcastCampaign.next_run_at.asc())
                )
                res = await session.execute(stmt)
                due_campaigns = res.scalars().all()

                if not due_campaigns:
                    return

                logger.info(f"Found {len(due_campaigns)} due broadcast campaign(s) to dispatch.")

                for campaign in due_campaigns:
                    await execute_campaign_broadcast(campaign.id, session)

            except Exception as e:
                logger.error(f"Error in broadcast campaign scan: {e}")


def start_scheduler() -> None:
    """Initialize and start the background broadcast scheduler."""
    global scheduler
    if scheduler and scheduler.running:
        logger.info("Scheduler already active.")
        return

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        check_and_dispatch_scheduled_campaigns,
        trigger=IntervalTrigger(seconds=settings.SCHEDULER_INTERVAL_SECONDS),
        id="broadcast_campaign_dispatcher",
        name="Arovida Doctor Broadcast Campaign Dispatcher",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info(f"Broadcast scheduler started. Polling every {settings.SCHEDULER_INTERVAL_SECONDS}s.")


def stop_scheduler() -> None:
    """Gracefully stop the background scheduler."""
    global scheduler
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Broadcast scheduler stopped.")


def is_scheduler_running() -> bool:
    """Check if scheduler is currently running."""
    global scheduler
    return bool(scheduler and scheduler.running)
