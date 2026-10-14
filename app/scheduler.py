"""
Automated Doctor Engagement & Broadcast Scheduler for Arovida PET-CT Centre.
Coordinates flexible recurring broadcasts (every Monday, 1st & 16th, daily, 1-click blasts),
applies Meta Cloud API rate-limiting delays (0.2s–0.5s pause), and logs dispatch results.
"""
import asyncio
import calendar
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
    cron_schedule: Optional[str] = None,
    scheduled_time: str = "10:00",
    from_dt: Optional[datetime] = None,
    frequency_type: Optional[str] = None,
    interval_days: Optional[int] = None,
    scheduled_day_of_week: Optional[str] = None,
    scheduled_month_dates: Optional[str] = None,
) -> Optional[datetime]:
    """
    Calculate the next execution timestamp based on flexible dynamic recurring rules:
    - EVERY_X_DAYS: Runs every X days (e.g. 10, 14, 21 days) at scheduled_time
    - WEEKLY_DAY: Specific day of the week (Monday through Sunday) at scheduled_time
    - MONTHLY_DATES: Specific dates of the month (e.g. "1, 16" or "5, 20") at scheduled_time
    - ONE_TIME: Immediate blast / single trigger
    Also supports backward-compatible cron_schedule values ('every_monday', 'dates_1_and_16', 'daily', etc.).
    """
    base_dt = from_dt or datetime.now(timezone.utc)
    hour, minute = parse_scheduled_time(scheduled_time)

    # Determine frequency type
    freq = (frequency_type or "").strip().upper()
    sched = (cron_schedule or "").strip().lower()

    if not freq:
        if sched in ("every_monday", "weekly"):
            freq = "WEEKLY_DAY"
            scheduled_day_of_week = "monday"
        elif sched == "every_friday":
            freq = "WEEKLY_DAY"
            scheduled_day_of_week = "friday"
        elif sched == "dates_1_and_16":
            freq = "MONTHLY_DATES"
            scheduled_month_dates = "1, 16"
        elif sched == "daily":
            freq = "EVERY_X_DAYS"
            interval_days = 1
        elif sched == "immediate":
            freq = "ONE_TIME"
        else:
            freq = "WEEKLY_DAY"

    if freq == "ONE_TIME":
        return base_dt

    if freq == "EVERY_X_DAYS":
        days = max(1, interval_days or 1)
        candidate = base_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= base_dt:
            candidate += timedelta(days=days)
        return candidate

    if freq == "WEEKLY_DAY":
        weekday_map = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6
        }
        day_str = (scheduled_day_of_week or "monday").strip().lower()
        target_weekday = weekday_map.get(day_str, 0)

        candidate = base_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        days_ahead = (target_weekday - base_dt.weekday()) % 7
        if days_ahead == 0 and candidate <= base_dt:
            days_ahead = 7
        return candidate + timedelta(days=days_ahead)

    if freq == "MONTHLY_DATES":
        dates_raw = (scheduled_month_dates or "1, 16").replace("and", ",").replace("&", ",")
        valid_dates = []
        for part in dates_raw.split(","):
            part = part.strip()
            if part.isdigit():
                d = int(part)
                if 1 <= d <= 31:
                    valid_dates.append(d)
        if not valid_dates:
            valid_dates = [1, 16]
        valid_dates = sorted(list(set(valid_dates)))

        year = base_dt.year
        month = base_dt.month

        # Try current month
        _, max_days_curr = calendar.monthrange(year, month)
        for d in valid_dates:
            day = min(d, max_days_curr)
            cand = datetime(year, month, day, hour, minute, 0, tzinfo=timezone.utc)
            if cand > base_dt:
                return cand

        # Advance to next month
        next_month = month + 1 if month < 12 else 1
        next_year = year if month < 12 else year + 1
        _, max_days_next = calendar.monthrange(next_year, next_month)
        day = min(valid_dates[0], max_days_next)
        return datetime(next_year, next_month, day, hour, minute, 0, tzinfo=timezone.utc)

    # Fallback
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

    # Fetch active doctors for this centre deterministically
    doc_stmt = (
        select(DoctorContact)
        .where(
            DoctorContact.user_id == campaign.user_id,
            DoctorContact.is_active == True,
        )
        .order_by(DoctorContact.doctor_name.asc(), DoctorContact.id.asc())
    )
    doc_res = await session.execute(doc_stmt)
    active_doctors = doc_res.scalars().all()

    total_doctors = len(active_doctors)
    logger.info(f"Preparing Campaign [{campaign.campaign_title}] for {total_doctors} active doctors.")

    if total_doctors == 0:
        campaign.last_run_at = datetime.now(timezone.utc)
        campaign.total_recipients = 0
        campaign.last_dispatched_doctor_index = 0
        if campaign.schedule_type == "ONE_TIME" or campaign.frequency_type == "ONE_TIME":
            campaign.status = "COMPLETED"
            campaign.next_run_at = None
        else:
            campaign.next_run_at = calculate_next_run_time(
                cron_schedule=campaign.cron_schedule,
                scheduled_time=campaign.scheduled_time,
                from_dt=datetime.now(timezone.utc),
                frequency_type=campaign.frequency_type,
                interval_days=campaign.interval_days,
                scheduled_day_of_week=campaign.scheduled_day_of_week,
                scheduled_month_dates=campaign.scheduled_month_dates,
            )
        await session.commit()
        return {"success": True, "sent": 0, "failed": 0, "message": "No active doctors in directory"}

    # Determine batch boundaries using persistent state tracking
    start_idx = campaign.last_dispatched_doctor_index or 0
    if start_idx >= total_doctors:
        start_idx = 0

    batch_size = max(1, campaign.batch_size or 20)
    end_idx = min(start_idx + batch_size, total_doctors)
    batch_doctors = active_doctors[start_idx:end_idx]

    logger.info(
        f"Campaign [{campaign.campaign_title}]: Executing batch doctors {start_idx + 1} to {end_idx} "
        f"of {total_doctors} (Batch size: {len(batch_doctors)}, Jitter Delay: {campaign.message_delay_seconds}s)."
    )

    # Fetch tenant WhatsApp settings and active WhatsApp Web QR session
    from app.services.whatsapp_web_session import is_session_connected, get_connected_phone
    tenant_settings = await get_user_settings_dict(session, campaign.user_id)
    raw_gateway_mode = (
        tenant_settings.get("whatsapp_gateway_type")
        or tenant_settings.get("gateway_mode")
        or settings.WHATSAPP_GATEWAY_TYPE
        or "greenapi"
    ).strip().lower()

    # Pre-load Green-API and Meta credentials
    greenapi_id = (
        tenant_settings.get("greenapi_id_instance")
        or settings.GREENAPI_ID_INSTANCE
        or "710722761375"
    ).strip()
    greenapi_token = (
        tenant_settings.get("greenapi_token")
        or settings.GREENAPI_TOKEN
        or "d8dd5c9670e143359a359e3c83daa38b167180dbe7314e2aa4"
    ).strip()

    phone_id = (tenant_settings.get("meta_phone_number_id") or settings.META_PHONE_NUMBER_ID or "").strip()
    access_token = (tenant_settings.get("meta_whatsapp_token") or settings.META_WHATSAPP_TOKEN or "").strip()

    web_is_connected = is_session_connected()
    linked_phone = get_connected_phone()

    # Determine active gateway mode:
    # 1. Green-API: Real WhatsApp Delivery via Linked Phone (Primary Default)
    if raw_gateway_mode in ("greenapi", "green_api", "green", "green-api") or (
        greenapi_id and greenapi_token and not (phone_id and access_token) and not web_is_connected
    ):
        gateway_mode = "greenapi"
        gateway = get_whatsapp_gateway("greenapi")
        phone_id = greenapi_id
        access_token = greenapi_token
        logger.info(f"[DISPATCH ROUTER] Mode: greenapi. Delivering via Green-API instance {greenapi_id}.")

    # 2. WhatsApp Web QR Gateway
    elif raw_gateway_mode in ("whatsapp_web_qr", "web", "whatsapp_web", "baileys", "qr", "web_qr"):
        gateway_mode = "whatsapp_web_qr"
        gateway = get_whatsapp_gateway("whatsapp_web_qr")
        logger.info(
            f"[DISPATCH ROUTER] Mode: whatsapp_web_qr. Bypassing Meta credentials completely. "
            f"Active session: {linked_phone or 'Connected'}."
        )

    # 3. Mock / Dry-Run Mode
    elif raw_gateway_mode in ("mock", "simulated", "dry_run"):
        gateway_mode = "mock"
        gateway = get_whatsapp_gateway("mock")
        logger.info("[DISPATCH ROUTER] Mode: mock. Delivery will be simulated as SUCCESS.")

    # 4. Meta WhatsApp Cloud API
    else:
        gateway_mode = "meta_cloud_api"
        if not phone_id or not access_token:
            err_msg = (
                "Missing Meta WhatsApp Cloud API credentials. Please set Phone Number ID and "
                "WhatsApp Access Token in Settings, or use Green-API Gateway."
            )
            logger.error(f"[DISPATCH ROUTER] {err_msg}")
            campaign.last_run_at = datetime.now(timezone.utc)
            campaign.total_recipients = total_doctors
            campaign.total_failed = (campaign.total_failed or 0) + len(batch_doctors)
            for doctor in batch_doctors:
                log_entry = BroadcastLog(
                    user_id=campaign.user_id,
                    campaign_id=campaign.id,
                    doctor_id=doctor.id,
                    doctor_name=doctor.doctor_name,
                    phone_number=doctor.phone_number,
                    message_snippet=campaign.template_message[:160],
                    sent_at=datetime.now(timezone.utc),
                    status="FAILED",
                    error_message=err_msg,
                    whatsapp_message_id=None,
                )
                session.add(log_entry)
            await session.commit()
            return {"success": False, "error": err_msg, "sent": 0, "failed": len(batch_doctors)}
        else:
            gateway = get_whatsapp_gateway("meta_cloud_api")
            logger.info(f"[DISPATCH ROUTER] Mode: meta_cloud_api. Verified credentials for Phone ID: {phone_id[:4]}***.")

    sent_count = 0
    failed_count = 0
    now_utc = datetime.now(timezone.utc)

    for index, doctor in enumerate(batch_doctors):
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

        # 4. Anti-Ban Human Jitter Delay between individual messages (10s–60s slider with ±15% natural jitter)
        if index < len(batch_doctors) - 1:
            base_delay = campaign.message_delay_seconds if (campaign.message_delay_seconds and campaign.message_delay_seconds >= 1.0) else 30.0
            jitter_delay = base_delay * random.uniform(0.85, 1.15)
            logger.debug(f"Anti-ban human jitter: sleeping {jitter_delay:.2f}s before next recipient.")
            await asyncio.sleep(jitter_delay)

    # Update campaign statistics & state tracking
    campaign.last_run_at = datetime.now(timezone.utc)
    campaign.total_recipients = total_doctors
    campaign.total_sent = (campaign.total_sent or 0) + sent_count
    campaign.total_failed = (campaign.total_failed or 0) + failed_count
    campaign.last_dispatched_doctor_index = end_idx

    # Check if more doctors remain in this batch sequence
    if end_idx < total_doctors:
        # More batches remain in pipeline! Wait batch_interval_minutes before resuming next batch
        pause_mins = max(1, campaign.batch_interval_minutes or 60)
        campaign.next_run_at = datetime.now(timezone.utc) + timedelta(minutes=pause_mins)
        campaign.status = "ACTIVE"
        logger.info(
            f"Campaign [{campaign.campaign_title}]: Batch completed ({sent_count} sent, {failed_count} failed). "
            f"Remaining: {total_doctors - end_idx} doctors. Next batch scheduled at {campaign.next_run_at} (in {pause_mins}m)."
        )
    else:
        # All doctors in directory have received this cycle!
        campaign.last_dispatched_doctor_index = 0
        if campaign.schedule_type == "ONE_TIME" or campaign.frequency_type == "ONE_TIME":
            campaign.status = "COMPLETED"
            campaign.next_run_at = None
            logger.info(f"Campaign [{campaign.campaign_title}] completed all {total_doctors} doctors. Marked COMPLETED.")
        else:
            campaign.next_run_at = calculate_next_run_time(
                cron_schedule=campaign.cron_schedule,
                scheduled_time=campaign.scheduled_time,
                from_dt=datetime.now(timezone.utc),
                frequency_type=campaign.frequency_type,
                interval_days=campaign.interval_days,
                scheduled_day_of_week=campaign.scheduled_day_of_week,
                scheduled_month_dates=campaign.scheduled_month_dates,
            )
            campaign.status = "ACTIVE"
            logger.info(
                f"Campaign [{campaign.campaign_title}] completed full cycle to all {total_doctors} doctors. "
                f"Next recurring cycle at {campaign.next_run_at}."
            )

    await session.commit()
    logger.info(f"Campaign [{campaign.campaign_title}] batch done: {sent_count} sent, {failed_count} failed.")

    return {
        "success": True,
        "campaign_id": campaign.id,
        "batch_sent": sent_count,
        "batch_failed": failed_count,
        "total_doctors": total_doctors,
        "last_index": campaign.last_dispatched_doctor_index,
        "remaining": max(0, total_doctors - end_idx),
        "status": campaign.status,
        "next_run_at": campaign.next_run_at.isoformat() if campaign.next_run_at else None,
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
