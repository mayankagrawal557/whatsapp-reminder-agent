"""
Broadcast Campaigns API Routes for Arovida PET-CT Centre.
Handles campaign composition, recurring scheduling (Mondays, 1st & 16th),
1-click blast dispatches, and Gemini AI copy polishing.
"""
import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import BroadcastCampaign, DoctorContact, User, get_db
from app.scheduler import calculate_next_run_time, execute_campaign_broadcast
from app.schemas import (
    CampaignCreate,
    CampaignResponse,
    GeminiPolishRequest,
    GeminiPolishResponse,
)
from app.services.gemini_service import gemini_service

logger = logging.getLogger("whatsapp_ai.routes.campaigns")
router = APIRouter(prefix="/api/campaigns", tags=["Broadcast Campaigns"])


@router.get("", response_model=List[CampaignResponse], summary="List all broadcast campaigns")
async def list_campaigns(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    List all doctor broadcast campaigns configured for the authenticated centre.
    """
    stmt = (
        select(BroadcastCampaign)
        .where(BroadcastCampaign.user_id == current_user.id)
        .order_by(BroadcastCampaign.created_at.desc())
    )
    res = await db.execute(stmt)
    return res.scalars().all()


@router.post("", response_model=CampaignResponse, status_code=status.HTTP_201_CREATED, summary="Create/Schedule broadcast campaign")
async def create_campaign(
    payload: CampaignCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a new Doctor Broadcast Campaign.
    If send_immediately is True, dispatches immediately via 1-click blast.
    Otherwise sets next_run_at based on recurring frequency (e.g. Every Monday at 10:00 AM).
    """
    now_utc = datetime.now(timezone.utc)

    # Compute recipient count
    doc_count_res = await db.execute(
        select(func.count(DoctorContact.id)).where(
            DoctorContact.user_id == current_user.id,
            DoctorContact.is_active == True,
        )
    )
    recipients_count = doc_count_res.scalar() or 0

    if payload.send_immediately or payload.schedule_type == "ONE_TIME":
        next_run = now_utc
    else:
        next_run = calculate_next_run_time(
            cron_schedule=payload.cron_schedule,
            scheduled_time=payload.scheduled_time,
            from_dt=now_utc,
        )

    campaign = BroadcastCampaign(
        user_id=current_user.id,
        campaign_title=payload.campaign_title.strip(),
        template_message=payload.template_message.strip(),
        schedule_type=payload.schedule_type,
        cron_schedule=payload.cron_schedule,
        scheduled_time=payload.scheduled_time or "10:00",
        next_run_at=next_run,
        status="ACTIVE",
        total_recipients=recipients_count,
        total_sent=0,
        total_failed=0,
    )
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)

    # If 1-click blast requested, execute immediately
    if payload.send_immediately:
        logger.info(f"Triggering immediate 1-click blast for campaign [{campaign.id}]")
        await execute_campaign_broadcast(campaign.id, db)
        await db.refresh(campaign)

    return campaign


@router.post("/{campaign_id}/blast-now", summary="Trigger immediate 1-click broadcast blast")
async def blast_now(
    campaign_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Instantly trigger execution of a campaign broadcast to all active doctors.
    """
    stmt = select(BroadcastCampaign).where(
        BroadcastCampaign.id == campaign_id,
        BroadcastCampaign.user_id == current_user.id,
    )
    res = await db.execute(stmt)
    camp = res.scalar_one_or_none()
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found.")

    blast_result = await execute_campaign_broadcast(campaign_id, db)
    blast_result["message"] = (
        f"Broadcast blast completed: {blast_result.get('sent', 0)} sent, {blast_result.get('failed', 0)} failed."
    )
    return blast_result


@router.patch("/{campaign_id}/toggle-status", response_model=CampaignResponse, summary="Pause or Resume campaign")
async def toggle_campaign_status(
    campaign_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Toggle a campaign between ACTIVE and PAUSED.
    """
    stmt = select(BroadcastCampaign).where(
        BroadcastCampaign.id == campaign_id,
        BroadcastCampaign.user_id == current_user.id,
    )
    res = await db.execute(stmt)
    camp = res.scalar_one_or_none()
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found.")

    if camp.status == "ACTIVE":
        camp.status = "PAUSED"
    else:
        camp.status = "ACTIVE"
        if not camp.next_run_at or camp.next_run_at < datetime.now(timezone.utc):
            camp.next_run_at = calculate_next_run_time(camp.cron_schedule, camp.scheduled_time)

    await db.commit()
    await db.refresh(camp)
    return camp


@router.delete("/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a campaign")
async def delete_campaign(
    campaign_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Delete a broadcast campaign and its associated dispatch logs.
    """
    stmt = select(BroadcastCampaign).where(
        BroadcastCampaign.id == campaign_id,
        BroadcastCampaign.user_id == current_user.id,
    )
    res = await db.execute(stmt)
    camp = res.scalar_one_or_none()
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found.")

    await db.delete(camp)
    await db.commit()
    return None


@router.post("/polish", response_model=GeminiPolishResponse, summary="Polish message with Gemini AI")
async def polish_broadcast_copy(
    payload: GeminiPolishRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Polish doctor outreach copy using Google Gemini AI.
    Tones: professional, slots_update, clinical_case, festive.
    Preserves dynamic tags: {doctor_name}, {specialty}, {hospital}, {centre_name}.
    """
    centre_name = current_user.business_name or payload.centre_name or "Arovida PET-CT Centre"
    polished = await gemini_service.polish_doctor_broadcast(
        template_text=payload.template_text,
        tone=payload.tone,
        centre_name=centre_name,
    )
    return GeminiPolishResponse(polished_text=polished, tone=payload.tone)
