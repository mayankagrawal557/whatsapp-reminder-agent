"""
FastAPI route handlers for multi-tenant Client management.
Enables packaging and onboarding multiple paying businesses (clinics, salons, consultants).
"""
import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import Client, Reminder, get_db
from app.schemas import ClientCreate, ClientResponse

logger = logging.getLogger("whatsapp_ai.routes.clients")
router = APIRouter(prefix="/clients", tags=["Clients (Tenants)"])


@router.post(
    "",
    response_model=ClientResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new business client (Tenant)",
)
async def create_client(
    payload: ClientCreate,
    db: AsyncSession = Depends(get_db),
):
    """
    Onboard a new paying tenant (e.g., a dental clinic, beauty salon, or consultant).
    """
    stmt = select(Client).where(Client.id == payload.id)
    res = await db.execute(stmt)
    if res.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Client tenant with ID '{payload.id}' already exists.",
        )

    new_client = Client(
        id=payload.id,
        name=payload.name,
        business_type=payload.business_type,
        default_tone=payload.default_tone,
        phone_number_id=payload.phone_number_id,
        whatsapp_token=payload.whatsapp_token,
        api_key=payload.api_key,
        is_active=True,
    )
    db.add(new_client)
    await db.commit()
    await db.refresh(new_client)

    logger.info(f"Registered new client tenant: {new_client.id} - {new_client.name}")
    return new_client


@router.get(
    "",
    response_model=List[ClientResponse],
    summary="List all registered business tenants",
)
async def list_clients(
    db: AsyncSession = Depends(get_db),
):
    """
    List all active tenants with their total reminders count.
    """
    stmt = (
        select(Client, func.count(Reminder.id).label("total_reminders"))
        .outerjoin(Reminder, Reminder.client_id == Client.id)
        .group_by(Client.id)
        .order_by(Client.created_at.desc())
    )
    res = await db.execute(stmt)
    clients_with_counts = []
    for client, count in res.all():
        client_data = ClientResponse(
            id=client.id,
            name=client.name,
            business_type=client.business_type,
            default_tone=client.default_tone,
            phone_number_id=client.phone_number_id,
            is_active=client.is_active,
            created_at=client.created_at,
            total_reminders=count,
        )
        clients_with_counts.append(client_data)
    return clients_with_counts


@router.get(
    "/{client_id}",
    response_model=ClientResponse,
    summary="Retrieve tenant details",
)
async def get_client(
    client_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Fetch specific tenant profile and reminder volume.
    """
    stmt = (
        select(Client, func.count(Reminder.id).label("total_reminders"))
        .outerjoin(Reminder, Reminder.client_id == Client.id)
        .where(Client.id == client_id)
        .group_by(Client.id)
    )
    res = await db.execute(stmt)
    row = res.first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Client '{client_id}' not found.",
        )
    client, count = row
    return ClientResponse(
        id=client.id,
        name=client.name,
        business_type=client.business_type,
        default_tone=client.default_tone,
        phone_number_id=client.phone_number_id,
        is_active=client.is_active,
        created_at=client.created_at,
        total_reminders=count,
    )
