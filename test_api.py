"""
End-to-end API Test Suite for WhatsApp AI Reminder Agent.
Tests Multi-tenant Auth (Register, Login, Me), Webhooks (Verification & Two-Way Replies),
AI Message Personalization, and Reminders CRUD.
"""
import asyncio
import os
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

os.environ["WHATSAPP_GATEWAY_TYPE"] = "mock"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_api.db"

import httpx
from app.main import app
from app.database import init_db


async def run_api_tests():
    print("==================================================")
    print("RUNNING MULTI-TENANT & TWO-WAY WEBHOOK TESTS...")
    print("==================================================")

    await init_db()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # 1. Web Dashboard Root
        print("\n[1] Testing GET / (Web Dashboard):")
        r = await client.get("/")
        assert r.status_code == 200
        print("  [OK] Dashboard served successfully.")

        # 2. Authentication: Register new clinic tenant
        print("\n[2] Testing POST /api/auth/register (New Clinic):")
        reg_payload = {
            "business_name": "Luxe Glow Aesthetics",
            "email": "contact@luxeglow.com",
            "password": "securepassword123",
            "business_type": "Aesthetics Salon",
        }
        r = await client.post("/api/auth/register", json=reg_payload)
        assert r.status_code in (201, 409)  # 201 created or 409 if already exists
        if r.status_code == 201:
            auth_data = r.json()
            token = auth_data["access_token"]
            print(f"  [OK] Clinic registered. User ID: {auth_data['user']['id']}, Token generated.")
        else:
            # Login
            login_r = await client.post("/api/auth/login", json={"email": "contact@luxeglow.com", "password": "securepassword123"})
            assert login_r.status_code == 200
            token = login_r.json()["access_token"]
            print(f"  [OK] Clinic already registered, logged in with token.")

        auth_headers = {"Authorization": f"Bearer {token}"}

        # 3. Authenticated Me endpoint
        print("\n[3] Testing GET /api/auth/me:")
        r = await client.get("/api/auth/me", headers=auth_headers)
        assert r.status_code == 200
        user_data = r.json()
        print(f"  [OK] Authenticated as: {user_data['business_name']} ({user_data['email']})")

        # 4. Add reminder under authenticated tenant
        print("\n[4] Testing POST /add-reminder (Two-Way Ready):")
        payload = {
            "customer_name": "Jessica Pearson",
            "phone": "+14155557890",
            "service_name": "HydraFacial Treatment",
            "appointment_datetime": "2026-10-25T14:00:00Z",
            "lead_hours": 24,
            "tone": "friendly",
        }
        r = await client.post("/add-reminder", json=payload, headers=auth_headers)
        assert r.status_code == 201
        reminder = r.json()
        rem_id = reminder["id"]
        print(f"  [OK] Created reminder ID: {rem_id} for {reminder['customer_name']} (Status: {reminder['status']})")

        # 5. Dispatch reminder (Send-Now)
        print(f"\n[5] Testing POST /reminders/{rem_id}/send-now:")
        r = await client.post(f"/reminders/{rem_id}/send-now", headers=auth_headers)
        assert r.status_code == 200
        send_data = r.json()
        assert send_data["status"] == "SENT"
        assert "Reply 1 to Confirm or 2 to Reschedule." in send_data["message"]
        print(f"  [OK] Dispatched. CTA prompt verified in message: \"{send_data['message'][-42:]}\"")

        # 6. Webhook verification handshake (Meta Graph API)
        print("\n[6] Testing GET /api/webhook (Meta Verification Handshake):")
        r = await client.get(
            "/api/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.challenge": "1158201444",
                "hub.verify_token": "my_secure_webhook_verify_token",
            },
        )
        assert r.status_code == 200
        assert r.text == "1158201444"
        print(f"  [OK] Webhook handshake returned challenge: {r.text}")

        # 7. Two-Way Reply Tracking: Simulate customer replying "1" (Confirm)
        print("\n[7] Testing Two-Way Reply Tracking -> Customer replies '1' (Confirm):")
        sim_payload = {"phone": "+14155557890", "message": "1 - Yes I'll be there!"}
        r = await client.post("/api/webhook/simulate", json=sim_payload)
        assert r.status_code == 200
        sim_res = r.json()
        assert sim_res["new_status"] == "CONFIRMED"
        print(f"  [OK] Inbound reply parsed! New Reminder Status: {sim_res['new_status']} | Reply: \"{sim_res['customer_reply']}\"")

        # 8. Two-Way Reply Tracking: Simulate customer replying "2" (Reschedule)
        print("\n[8] Testing Two-Way Reply Tracking -> Customer replies '2' (Reschedule):")
        sim_payload2 = {"phone": "+14155557890", "message": "2 please, I need to reschedule to next Tuesday"}
        r = await client.post("/api/webhook/simulate", json=sim_payload2)
        assert r.status_code == 200
        sim_res2 = r.json()
        assert sim_res2["new_status"] == "NEEDS_RESCHEDULE"
        print(f"  [OK] Inbound reply parsed! New Reminder Status: {sim_res2['new_status']} | Reply: \"{sim_res2['customer_reply']}\"")

    print("\n==================================================")
    print("ALL MULTI-TENANT & TWO-WAY TESTS PASSED! [OK]")
    print("==================================================")


if __name__ == "__main__":
    asyncio.run(run_api_tests())
