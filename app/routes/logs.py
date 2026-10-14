"""
Dispatch History & Analytics API Routes for Arovida PET-CT Centre.
Provides delivery audits, KPI stats, and campaign execution records.
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import BroadcastCampaign, BroadcastLog, DoctorContact, User, get_db
from app.schemas import BroadcastLogResponse, BroadcastStatsResponse

logger = logging.getLogger("whatsapp_ai.routes.logs")
router = APIRouter(prefix="/api/logs", tags=["Dispatch History & Stats"])


@router.get("", response_model=List[BroadcastLogResponse], summary="List dispatch logs")
async def get_broadcast_logs(
    limit: int = Query(100, ge=1, le=500),
    status: Optional[str] = Query(None, description="Filter by status: SENT or FAILED"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    List past broadcast dispatches with recipient doctor name, phone, status, and error details.
    """
    stmt = select(BroadcastLog).where(BroadcastLog.user_id == current_user.id)

    if status and status.upper() in ("SENT", "FAILED"):
        stmt = stmt.where(BroadcastLog.status == status.upper())

    stmt = stmt.order_by(BroadcastLog.sent_at.desc()).limit(limit)
    res = await db.execute(stmt)
    return res.scalars().all()


@router.get("/stats", response_model=BroadcastStatsResponse, summary="Get summary KPI stats")
async def get_broadcast_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Retrieve real-time aggregate statistics for the executive dashboard:
    total doctors, active doctors, campaigns, broadcasts delivered, and success rate.
    """
    # Doctors
    doc_res = await db.execute(
        select(
            func.count(DoctorContact.id),
            func.count(DoctorContact.id).filter(DoctorContact.is_active == True),
        ).where(DoctorContact.user_id == current_user.id)
    )
    total_docs, active_docs = doc_res.first() or (0, 0)

    # Campaigns
    camp_res = await db.execute(
        select(
            func.count(BroadcastCampaign.id),
            func.count(BroadcastCampaign.id).filter(BroadcastCampaign.status == "ACTIVE"),
        ).where(BroadcastCampaign.user_id == current_user.id)
    )
    total_camps, active_camps = camp_res.first() or (0, 0)

    # Broadcast Logs
    log_res = await db.execute(
        select(
            func.count(BroadcastLog.id).filter(BroadcastLog.status == "SENT"),
            func.count(BroadcastLog.id).filter(BroadcastLog.status == "FAILED"),
        ).where(BroadcastLog.user_id == current_user.id)
    )
    sent_count, failed_count = log_res.first() or (0, 0)

    total_dispatched = sent_count + failed_count
    delivery_rate = round((sent_count / total_dispatched * 100), 1) if total_dispatched > 0 else 100.0

    return BroadcastStatsResponse(
        total_doctors=total_docs or 0,
        active_doctors=active_docs or 0,
        total_campaigns=total_camps or 0,
        active_campaigns=active_camps or 0,
        total_broadcasts_sent=sent_count or 0,
        total_broadcasts_failed=failed_count or 0,
        delivery_rate_pct=delivery_rate,
    )
