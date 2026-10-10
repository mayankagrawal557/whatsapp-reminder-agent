"""
FastAPI route handlers for appointment reminders:
- /add-reminder: Single reminder ingestion (isolated to current clinic)
- /bulk-upload: CSV and Excel (.xlsx) file upload handler (isolated to current clinic)
- /api/ai/preview: Live AI personalized message preview endpoint
- /reminders: List and query reminders with status, search & clinic filters
- /reminders/{id}/send-now: Dispatch a single reminder immediately from dashboard
- /reminders/{id}: Cancel/Delete reminder
- /sample-csv: Download ready-to-use sample CSV template
- /dispatch-now: Immediate trigger for testing and demos
"""
import csv
import io
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.database import (
    Client,
    MessageAuditLog,
    Reminder,
    User,
    get_db,
    get_user_settings_dict,
)
from app.schemas import (
    AIPreviewRequest,
    AIPreviewResponse,
    BulkUploadResponse,
    BulkUploadRowError,
    ReminderCreate,
    ReminderResponse,
    normalize_phone_number,
)
from app.services.gemini_service import gemini_service
from app.services.whatsapp_service import get_whatsapp_gateway

logger = logging.getLogger("whatsapp_ai.routes.reminders")
router = APIRouter(tags=["Reminders"])


def _calculate_reminder_time(appointment_dt: datetime, reminder_time: Optional[datetime], lead_hours: Optional[int]) -> datetime:
    if appointment_dt.tzinfo is None:
        appointment_dt = appointment_dt.replace(tzinfo=timezone.utc)

    if reminder_time is not None:
        if reminder_time.tzinfo is None:
            return reminder_time.replace(tzinfo=timezone.utc)
        return reminder_time

    hours = lead_hours if lead_hours is not None else settings.DEFAULT_REMINDER_LEAD_HOURS
    return appointment_dt - timedelta(hours=hours)


@router.post(
    "/api/ai/preview",
    response_model=AIPreviewResponse,
    summary="Live test Gemini message generation before sending",
)
async def preview_ai_message(
    payload: AIPreviewRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stored = await get_user_settings_dict(db, current_user.id)
    biz_name = payload.business_name or stored.get("business_name") or current_user.business_name
    biz_type = payload.business_type or stored.get("business_type") or current_user.business_type
    api_key = stored.get("gemini_api_key") or settings.GEMINI_API_KEY
    model = stored.get("gemini_model") or settings.GEMINI_MODEL

    appt_dt = payload.appointment_datetime or (datetime.now(timezone.utc) + timedelta(days=1, hours=2))

    generated = await gemini_service.generate_reminder(
        customer_name=payload.customer_name,
        appointment_datetime=appt_dt,
        service_name=payload.service_name,
        notes=payload.notes,
        tone=payload.tone or "friendly",
        business_name=biz_name,
        business_type=biz_type,
        custom_api_key=api_key,
        model_name=model,
    )

    formatted_time = gemini_service.format_datetime_friendly(appt_dt)
    return AIPreviewResponse(
        message=generated,
        tone=payload.tone or "friendly",
        customer_name=payload.customer_name,
        formatted_time=formatted_time,
    )


@router.post(
    "/add-reminder",
    response_model=ReminderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a single appointment reminder",
)
async def add_reminder(
    payload: ReminderCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    reminder_dispatch_time = _calculate_reminder_time(
        payload.appointment_datetime,
        payload.reminder_time,
        payload.lead_hours,
    )

    new_id = str(uuid.uuid4())
    new_reminder = Reminder(
        id=new_id,
        user_id=current_user.id,
        client_id=payload.client_id or current_user.id,
        customer_name=payload.customer_name.strip(),
        phone=payload.phone,
        service_name=payload.service_name.strip(),
        appointment_datetime=payload.appointment_datetime,
        reminder_time=reminder_dispatch_time,
        notes=payload.notes.strip() if payload.notes else None,
        tone=payload.tone,
        status="PENDING",
    )

    db.add(new_reminder)
    await db.commit()
    await db.refresh(new_reminder)

    logger.info(f"Created reminder [{new_reminder.id}] for {new_reminder.customer_name} under user [{current_user.id}]")
    return new_reminder


@router.post(
    "/bulk-upload",
    response_model=BulkUploadResponse,
    summary="Bulk upload reminders via CSV or Excel (.xlsx) file",
)
async def bulk_upload_file(
    file: UploadFile = File(..., description="CSV or Excel file with columns [name, phone, appointment_datetime, service_name, notes, tone]"),
    client_id: Optional[str] = Form(None),
    lead_hours: Optional[int] = Form(24),
    default_tone: Optional[str] = Form("friendly"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    filename_lower = file.filename.lower()
    content_bytes = await file.read()
    rows_data: List[dict] = []

    if filename_lower.endswith(".csv"):
        try:
            decoded_text = content_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            decoded_text = content_bytes.decode("latin-1")

        reader = csv.DictReader(io.StringIO(decoded_text))
        if not reader.fieldnames:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="CSV file is empty or missing headers.")
        for r in reader:
            rows_data.append(dict(r))

    elif filename_lower.endswith(".xlsx") or filename_lower.endswith(".xls"):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(content_bytes), data_only=True)
            sheet = wb.active
            headers = [cell.value for cell in sheet[1] if cell.value is not None]
            if not headers:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Excel file is empty.")

            for row in sheet.iter_rows(min_row=2, values_only=True):
                if any(row):
                    row_dict = {}
                    for col_idx, header in enumerate(headers):
                        if col_idx < len(row):
                            val = row[col_idx]
                            row_dict[str(header)] = str(val) if val is not None else ""
                    rows_data.append(row_dict)
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Failed to read Excel file: {str(exc)}")
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File must be a .csv or .xlsx file.")

    total_received = 0
    inserted = 0
    errors: List[BulkUploadRowError] = []
    created_ids: List[str] = []

    for row_idx, row in enumerate(rows_data, start=1):
        total_received += 1
        lower_row = {str(k).strip().lower(): v for k, v in row.items() if k is not None}

        def get_val(keys: list) -> Optional[str]:
            for k in keys:
                if k in lower_row and lower_row[k]:
                    val = str(lower_row[k]).strip()
                    if val and val != "None":
                        return val
            return None

        name = get_val(["name", "customer_name", "client_name", "full_name", "patient_name"])
        phone_raw = get_val(["phone", "mobile", "contact", "whatsapp", "phone_number"])
        dt_raw = get_val(["appointment_datetime", "appointment_time", "datetime", "date", "event_time", "time"])
        service = get_val(["service_name", "service", "treatment", "type", "appointment_type"])
        notes = get_val(["notes", "note", "instructions", "details"])
        row_tone = get_val(["tone", "message_tone", "tone_preference"]) or default_tone
        row_lead_hours_str = get_val(["lead_hours", "lead_time_hours"])

        if not name or not phone_raw or not dt_raw or not service:
            missing = []
            if not name: missing.append("name")
            if not phone_raw: missing.append("phone")
            if not dt_raw: missing.append("appointment_datetime")
            if not service: missing.append("service_name")
            errors.append(
                BulkUploadRowError(
                    row_number=row_idx,
                    data=row,
                    error=f"Missing required field(s): {', '.join(missing)}",
                )
            )
            continue

        try:
            clean_phone = normalize_phone_number(phone_raw, default_country_code=settings.DEFAULT_COUNTRY_CODE)
        except Exception as e:
            errors.append(BulkUploadRowError(row_number=row_idx, data=row, error=f"Invalid phone: {e}"))
            continue

        try:
            appt_dt = ReminderCreate.parse_appointment_datetime(dt_raw)
        except Exception as e:
            errors.append(BulkUploadRowError(row_number=row_idx, data=row, error=f"Invalid appointment datetime: {e}"))
            continue

        try:
            row_lead = int(row_lead_hours_str) if row_lead_hours_str else (lead_hours or 24)
        except ValueError:
            row_lead = 24
        dispatch_dt = _calculate_reminder_time(appt_dt, None, row_lead)

        rem_id = str(uuid.uuid4())
        reminder_obj = Reminder(
            id=rem_id,
            user_id=current_user.id,
            client_id=client_id or current_user.id,
            customer_name=name,
            phone=clean_phone,
            service_name=service,
            appointment_datetime=appt_dt,
            reminder_time=dispatch_dt,
            notes=notes,
            tone=row_tone,
            status="PENDING",
        )
        db.add(reminder_obj)
        created_ids.append(rem_id)
        inserted += 1

    if inserted > 0:
        await db.commit()

    return BulkUploadResponse(
        total_received=total_received,
        inserted=inserted,
        failed=len(errors),
        errors=errors,
        reminder_ids=created_ids,
    )


@router.get("/sample-csv", summary="Download sample CSV template")
async def download_sample_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["name", "phone", "appointment_datetime", "service_name", "notes", "tone", "lead_hours"])
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d 14:30:00")
    day_after = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d 10:00:00")
    writer.writerow(["Sarah Jenkins", "+14155552671", tomorrow, "Teeth Cleaning & Polish", "Please fast 2 hours prior", "friendly", "24"])
    writer.writerow(["David Miller", "+14155552672", day_after, "Tax Consultation", "Bring last year financial forms", "formal", "48"])
    writer.writerow(["Elena Rostova", "+14155552673", tomorrow, "Balayage Hair Styling", "Arrive with clean dry hair", "friendly", "12"])
    writer.writerow(["Marcus Vance", "+14155552674", tomorrow, "Pre-Op Evaluation", "Critical consultation before procedure", "urgent", "24"])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=whatsapp_reminders_template.csv"},
    )


@router.get("/export-reminders", summary="Export appointments to CSV for receptionists")
@router.get("/api/reminders/export", summary="Export appointments to CSV for receptionists")
async def export_reminders_csv(
    status: Optional[str] = Query(None, description="Status filter (e.g. ALL, CONFIRMED, NEEDS_RESCHEDULE)"),
    q: Optional[str] = Query(None, description="Search query"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Generate and stream a receptionist-ready CSV file of booked appointments,
    customer replies, and delivery statuses.
    """
    stmt = (
        select(Reminder)
        .where(Reminder.user_id == current_user.id)
        .order_by(Reminder.appointment_datetime.asc())
    )

    if status and status.upper() != "ALL":
        # Supports comma-separated statuses like CONFIRMED,NEEDS_RESCHEDULE
        statuses = [s.strip().upper() for s in status.split(",") if s.strip()]
        if len(statuses) == 1:
            stmt = stmt.where(Reminder.status == statuses[0])
        elif len(statuses) > 1:
            stmt = stmt.where(Reminder.status.in_(statuses))

    if q and q.strip():
        term = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Reminder.customer_name.ilike(term),
                Reminder.phone.ilike(term),
                Reminder.service_name.ilike(term),
            )
        )

    res = await db.execute(stmt)
    reminders = res.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Customer Name",
        "Phone Number",
        "Service / Treatment",
        "Appointment Date & Time",
        "Status",
        "Customer WhatsApp Reply",
        "Reply Timestamp",
        "Dispatched At",
        "Preparation Notes",
    ])

    for r in reminders:
        appt_str = r.appointment_datetime.strftime("%Y-%m-%d %H:%M") if r.appointment_datetime else ""
        reply_str = r.customer_reply or "No reply"
        replied_at_str = r.replied_at.strftime("%Y-%m-%d %H:%M") if r.replied_at else ""
        sent_at_str = r.sent_at.strftime("%Y-%m-%d %H:%M") if r.sent_at else ""
        writer.writerow([
            r.customer_name,
            r.phone,
            r.service_name,
            appt_str,
            r.status,
            reply_str,
            replied_at_str,
            sent_at_str,
            r.notes or "",
        ])

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"receptionist_reminders_{timestamp}.csv"
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get(
    "/reminders",
    response_model=List[ReminderResponse],
    summary="List reminders isolated to logged in clinic",
)
async def list_reminders(
    status: Optional[str] = Query(None, description="Filter by status"),
    q: Optional[str] = Query(None, description="Search term across name, phone, or service"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Reminder)
        .where(Reminder.user_id == current_user.id)
        .order_by(Reminder.appointment_datetime.asc())
    )

    if status and status.upper() != "ALL":
        stmt = stmt.where(Reminder.status == status.upper())
    if q and q.strip():
        term = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Reminder.customer_name.ilike(term),
                Reminder.phone.ilike(term),
                Reminder.service_name.ilike(term),
            )
        )

    stmt = stmt.offset(offset).limit(limit)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post(
    "/reminders/{reminder_id}/send-now",
    summary="Trigger immediate AI generation and WhatsApp dispatch",
)
async def send_reminder_now(
    reminder_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Reminder).where(Reminder.id == reminder_id, Reminder.user_id == current_user.id)
    res = await db.execute(stmt)
    reminder = res.scalar_one_or_none()

    if not reminder:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reminder not found.")

    stored = await get_user_settings_dict(db, current_user.id)
    biz_name = stored.get("business_name") or current_user.business_name or settings.DEFAULT_BUSINESS_NAME
    biz_type = stored.get("business_type") or current_user.business_type or settings.DEFAULT_BUSINESS_TYPE
    active_tone = reminder.tone or stored.get("default_tone") or "friendly"
    phone_id = stored.get("meta_phone_number_id") or settings.META_PHONE_NUMBER_ID
    whatsapp_token = stored.get("meta_whatsapp_token") or settings.META_WHATSAPP_TOKEN
    gateway_type = stored.get("whatsapp_gateway_type") or settings.WHATSAPP_GATEWAY_TYPE

    custom_text = await gemini_service.generate_reminder(
        customer_name=reminder.customer_name,
        appointment_datetime=reminder.appointment_datetime,
        service_name=reminder.service_name,
        notes=reminder.notes,
        tone=active_tone,
        business_name=biz_name,
        business_type=biz_type,
        custom_api_key=stored.get("gemini_api_key") or settings.GEMINI_API_KEY,
        model_name=stored.get("gemini_model") or settings.GEMINI_MODEL,
    )

    reminder.generated_message = custom_text
    reminder.attempts += 1

    active_gateway = get_whatsapp_gateway(gateway_type)
    send_result = await active_gateway.send_text_message(
        to_phone=reminder.phone,
        message=custom_text,
        phone_number_id=phone_id,
        access_token=whatsapp_token,
    )

    if send_result.success:
        reminder.status = "SENT"
        reminder.whatsapp_message_id = send_result.message_id
        reminder.sent_at = datetime.now(timezone.utc)
        reminder.error_log = None
    else:
        reminder.status = "FAILED"
        reminder.error_log = send_result.error_message

    audit = MessageAuditLog(
        user_id=current_user.id,
        client_id=reminder.client_id,
        reminder_id=reminder.id,
        direction="OUTBOUND",
        status=reminder.status,
        payload=custom_text,
    )
    db.add(audit)
    await db.commit()
    await db.refresh(reminder)

    return {
        "reminder_id": reminder.id,
        "success": send_result.success,
        "status": reminder.status,
        "message": custom_text,
        "whatsapp_message_id": send_result.message_id,
        "error": send_result.error_message if not send_result.success else None,
    }


@router.delete("/reminders/{reminder_id}", summary="Cancel or delete reminder")
async def delete_reminder(
    reminder_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Reminder).where(Reminder.id == reminder_id, Reminder.user_id == current_user.id)
    result = await db.execute(stmt)
    reminder = result.scalar_one_or_none()

    if not reminder:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reminder not found.")

    if reminder.status == "PROCESSING":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot cancel reminder actively in-flight.")

    await db.delete(reminder)
    await db.commit()
    return {"message": f"Reminder {reminder_id} deleted successfully."}


@router.post("/dispatch-now", summary="Trigger immediate scan")
async def dispatch_now():
    import asyncio
    from app.scheduler import dispatch_due_reminders_task
    asyncio.create_task(dispatch_due_reminders_task())
    return {"message": "Immediate background dispatch scan initiated successfully."}
