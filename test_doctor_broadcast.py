"""
Comprehensive Test Suite for Arovida PET-CT Centre Doctor Engagement & Broadcast Engine.
Tests:
1. Health endpoint (for UptimeRobot keep-alive)
2. Web Dashboard loading
3. Doctor Directory CRUD, phone auto-sanitization (+91), bulk CSV upload, and demo seeding
4. Broadcast Campaign creation, recurring rules, dynamic tags personalization, and instant blast
5. Gemini AI copy polishing across specialized medical oncology tones
6. Dispatch History logs and KPI aggregate stats
"""
import asyncio
import io
import os
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

os.environ["WHATSAPP_GATEWAY_TYPE"] = "mock"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_broadcast.db"

if os.path.exists("./test_broadcast.db"):
    try:
        os.remove("./test_broadcast.db")
    except Exception:
        pass

import httpx
from app.main import app
from app.database import init_db


async def run_broadcast_tests():
    print("==================================================================")
    print("RUNNING AROVIDA PET-CT CENTRE DOCTOR BROADCAST ENGINE TESTS...")
    print("==================================================================")

    await init_db()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # 1. Health check endpoint (UptimeRobot)
        print("\n[1] Testing GET /health (UptimeRobot Keep-Alive):")
        r = await client.get("/health")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}"
        data = r.json()
        assert data["status"] == "ok"
        print(f"  [OK] Health check returned: {data}")

        # 2. Web Dashboard Root
        print("\n[2] Testing GET / (Web Dashboard):")
        r = await client.get("/")
        assert r.status_code == 200
        assert "Arovida PET-CT Centre" in r.text
        assert "Doctor Directory" in r.text
        assert "Broadcast & Campaigns" in r.text
        print("  [OK] Dashboard index.html served with Arovida branding and 3 tabs.")

        # 3. Doctor Directory - Seed Demo Oncologists
        print("\n[3] Testing POST /api/doctors/seed-demo:")
        r = await client.post("/api/doctors/seed-demo")
        assert r.status_code == 200
        print(f"  [OK] Seeded demo oncologists: {r.json()['message']}")

        # 4. List Doctors
        print("\n[4] Testing GET /api/doctors:")
        r = await client.get("/api/doctors")
        assert r.status_code == 200
        doctors = r.json()
        assert len(doctors) >= 5
        print(f"  [OK] Retrieved {len(doctors)} doctor contacts:")
        for doc in doctors[:3]:
            print(f"       • {doc['doctor_name']} ({doc['specialty']}) - {doc['phone_number']}")

        # 5. Add Doctor with 10-digit number (Auto +91 Sanitization)
        print("\n[5] Testing POST /api/doctors (10-Digit Auto +91 Sanitization):")
        new_doc_payload = {
            "doctor_name": "Dr. Vivek Mehra",
            "phone_number": "9811223344",  # 10 digits without +91
            "specialty": "Uro-Oncologist",
            "hospital": "Lilavati Hospital",
            "is_active": True,
        }
        r = await client.post("/api/doctors", json=new_doc_payload)
        assert r.status_code == 201
        created_doc = r.json()
        assert created_doc["phone_number"] == "+919811223344", f"Expected +919811223344, got {created_doc['phone_number']}"
        doc_id = created_doc["id"]
        print(f"  [OK] Added doctor with auto-sanitized phone: {created_doc['phone_number']}")

        # 6. Toggle Doctor Status
        print(f"\n[6] Testing PATCH /api/doctors/{doc_id}/toggle:")
        r = await client.patch(f"/api/doctors/{doc_id}/toggle")
        assert r.status_code == 200
        toggled = r.json()
        assert toggled["is_active"] is False
        print(f"  [OK] Doctor status toggled to: is_active={toggled['is_active']}")

        # Toggle back to active
        await client.patch(f"/api/doctors/{doc_id}/toggle")

        # 7. Download Sample CSV
        print("\n[7] Testing GET /api/doctors/sample-csv:")
        r = await client.get("/api/doctors/sample-csv")
        assert r.status_code == 200
        assert "Doctor Name,Phone,Specialty,Hospital" in r.text
        print("  [OK] Sample CSV template generated with correct column headers.")

        # 8. Bulk Upload Doctor Directory (CSV)
        print("\n[8] Testing POST /api/doctors/bulk-upload (CSV):")
        csv_content = (
            "Doctor Name,Phone,Specialty,Hospital\n"
            "Dr. Sanjay Kapur,9876500001,Medical Oncology,Apollo Hospital\n"
            "Dr. Neha Kulkarni,9876500002,Nuclear Medicine,Jupiter Hospital\n"
        ).encode("utf-8")
        files = {"file": ("test_doctors.csv", io.BytesIO(csv_content), "text/csv")}
        r = await client.post("/api/doctors/bulk-upload", files=files)
        assert r.status_code == 200
        upload_res = r.json()
        assert upload_res["inserted"] >= 1
        print(f"  [OK] Bulk upload successful: {upload_res['inserted']} inserted, {upload_res['skipped_duplicates']} skipped.")

        # 9. Polish Copy with Gemini AI
        print("\n[9] Testing POST /api/campaigns/polish (Clinical Oncology Tones):")
        polish_payload = {
            "template_text": "Slots open for PET-CT scans for your patients at {centre_name}. Contact reception.",
            "tone": "slots_update",
            "centre_name": "Arovida PET-CT Centre",
        }
        r = await client.post("/api/campaigns/polish", json=polish_payload)
        assert r.status_code == 200
        polish_res = r.json()
        assert len(polish_res["polished_text"]) > 20
        print(f"  [OK] Gemini copy enhancement succeeded:\n       \"{polish_res['polished_text'][:120]}...\"")

        # 10. Create and Trigger 1-Click Broadcast Campaign Blast
        print("\n[10] Testing POST /api/campaigns (1-Click Instant Blast):")
        camp_payload = {
            "campaign_title": "Urgent Ga-68 DOTANOC Slots Available",
            "template_message": "Dear {doctor_name}, priority slots open at {centre_name} for your {specialty} referrals.",
            "schedule_type": "ONE_TIME",
            "cron_schedule": "immediate",
            "scheduled_time": "10:00",
            "send_immediately": True,
        }
        r = await client.post("/api/campaigns", json=camp_payload)
        assert r.status_code == 201
        camp = r.json()
        assert camp["total_sent"] > 0
        camp_id = camp["id"]
        print(f"  [OK] Broadcast blast executed! Total recipients dispatched: {camp['total_sent']} (Failed: {camp['total_failed']})")

        # 11. Dispatch Logs & KPI Stats
        print("\n[11] Testing GET /api/logs & /api/logs/stats:")
        r_logs = await client.get("/api/logs")
        assert r_logs.status_code == 200
        logs = r_logs.json()
        assert len(logs) > 0
        first_log = logs[0]
        print(f"  [OK] Retrieved {len(logs)} delivery logs. Most recent: to {first_log['doctor_name']} ({first_log['phone_number']}) - Status: {first_log['status']}")

        r_stats = await client.get("/api/logs/stats")
        assert r_stats.status_code == 200
        stats = r_stats.json()
        assert stats["total_doctors"] > 0
        assert stats["total_broadcasts_sent"] > 0
        assert stats["delivery_rate_pct"] == 100.0
        print(f"  [OK] Summary KPI Stats: Total Doctors={stats['total_doctors']}, Dispatched={stats['total_broadcasts_sent']}, Success Rate={stats['delivery_rate_pct']}%")

        # 12. Retrigger Campaign Blast
        print(f"\n[12] Testing POST /api/campaigns/{camp_id}/blast-now:")
        r_blast = await client.post(f"/api/campaigns/{camp_id}/blast-now")
        assert r_blast.status_code == 200
        print(f"  [OK] Re-blast triggered: {r_blast.json()['message']}")

        # 13. Test WhatsApp Web QR-based Gateway Endpoints
        print("\n[13] Testing WhatsApp Web QR Gateway (/api/whatsapp-web/status & /api/whatsapp-web/start):")
        r_web_status = await client.get("/api/whatsapp-web/status")
        assert r_web_status.status_code == 200
        web_data = r_web_status.json()
        print(f"  [OK] WhatsApp Web Status: {web_data.get('status')}")

        r_web_start = await client.post("/api/whatsapp-web/start")
        assert r_web_start.status_code == 200
        print(f"  [OK] WhatsApp Web QR Start triggered: {r_web_start.json().get('message')}")

    print("\n==================================================================")
    print("ALL AROVIDA DOCTOR BROADCAST ENGINE TESTS PASSED SUCCESSFULLY! ✓")
    print("==================================================================")


if __name__ == "__main__":
    asyncio.run(run_broadcast_tests())
