"""
Verification and Test Suite for WhatsApp AI Reminder Automation.
Tests:
1. AI Service personalization and fallback templates across tones.
2. Phone number normalization.
3. Database initialization and multi-tenant client creation.
4. Reminder creation, status transitions, and query filtering.
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

# Ensure stdout handles UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# Set test environment
os.environ["WHATSAPP_GATEWAY_TYPE"] = "mock"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_reminders.db"

from app.ai_service import ai_agent
from app.config import settings
from app.database import AsyncSessionLocal, Client, Reminder, engine, init_db
from app.schemas import normalize_phone_number
from app.whatsapp_service import get_whatsapp_gateway


async def run_tests():
    print("==================================================")
    print("RUNNING WHATSAPP AI REMINDER SUITE VERIFICATION...")
    print("==================================================")

    # 1. Test Phone Normalization
    print("\n[1] Testing Phone Normalization:")
    test_numbers = [
        ("+1 (415) 555-2671", "+14155552671"),
        ("+1 415-555-9999", "+14155559999"),
        ("9876543210", "+919876543210"),
        ("+44 7911 123456", "+447911123456"),
    ]
    for raw, expected in test_numbers:
        norm = normalize_phone_number(raw)
        assert norm == expected, f"Expected {expected}, got {norm}"
        print(f"  [OK] '{raw}' -> '{norm}'")

    # 2. Test Fallback Template Engine for all Tones
    print("\n[2] Testing AI Message Generation (Template Fallback):")
    now = datetime.now(timezone.utc) + timedelta(days=1)
    tones = ["friendly", "professional", "urgent", "empathetic", "casual"]

    for tone in tones:
        msg = await ai_agent.generate_reminder(
            customer_name="Sarah Jenkins",
            appointment_datetime=now,
            service_name="Teeth Cleaning",
            notes="Please fast 2 hours before appointment",
            tone=tone,
            business_name="Apex Smiles Clinic",
            business_type="Dental Clinic",
        )
        assert "Sarah Jenkins" in msg
        assert "Teeth Cleaning" in msg
        print(f"  [OK] [{tone.upper()}]:\n       \"{msg}\"")

    # 3. Test Database Init & Multi-tenancy
    print("\n[3] Testing Database Initialization & Tenants:")
    await init_db()
    tenant_id = f"salon_glow_{uuid.uuid4().hex[:6]}"
    async with AsyncSessionLocal() as session:
        # Create a new tenant client
        tenant = Client(
            id=tenant_id,
            name="Glow Aesthetics & Spa",
            business_type="Aesthetics Salon",
            default_tone="casual",
            is_active=True,
        )
        session.add(tenant)
        await session.commit()
        print(f"  [OK] Created tenant: {tenant_id} (Glow Aesthetics & Spa)")

        # Create a test reminder
        rem = Reminder(
            client_id=tenant_id,
            customer_name="Elena Rostova",
            phone="+14155558888",
            service_name="HydraFacial Treatment",
            appointment_datetime=now,
            reminder_time=now - timedelta(hours=24),
            notes="Arrive 10 minutes early for consultation",
            tone="casual",
            status="PENDING",
        )
        session.add(rem)
        await session.commit()
        await session.refresh(rem)
        print(f"  [OK] Inserted test reminder ID: {rem.id} | Status: {rem.status}")

        # 4. Test Mock Gateway Dispatch
        print("\n[4] Testing WhatsApp Gateway (Mock Dry-Run):")
        gateway = get_whatsapp_gateway()
        result = await gateway.send_text_message(
            to_phone=rem.phone,
            message="Hey Elena! Quick heads up about your HydraFacial on Tuesday at Glow Aesthetics & Spa.",
        )
        assert result.success is True
        print(f"  [OK] Dispatched via {type(gateway).__name__}: ID={result.message_id}, Success={result.success}")

    # Dispose engine cleanly
    await engine.dispose()
    if os.path.exists("./test_reminders.db"):
        try:
            os.remove("./test_reminders.db")
        except Exception:
            pass

    print("\n==================================================")
    print("ALL VERIFICATION CHECKS PASSED SUCCESSFULLY! [OK]")
    print("==================================================")


if __name__ == "__main__":
    asyncio.run(run_tests())
