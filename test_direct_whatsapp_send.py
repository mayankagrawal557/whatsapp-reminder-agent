#!/usr/bin/env python3
"""
Direct WhatsApp Dispatch Verification Test Script.
Sends a live message to recipient +919754119551 and logs the exact raw network response
from WhatsApp servers (Baileys WebSocket Bridge or Meta Graph API).

Usage:
    # Test local WhatsApp Web Baileys bridge
    python test_direct_whatsapp_send.py --to +919754119551 --gateway web

    # Test local Meta WhatsApp Cloud API
    python test_direct_whatsapp_send.py --to +919754119551 --gateway meta

    # Test against live deployed Render API
    python test_direct_whatsapp_send.py --to +919754119551 --url https://whatsapp-reminder-agent-zybo.onrender.com
"""
import argparse
import asyncio
import json
import sys
import httpx


async def run_remote_test(base_url: str, to: str, message: str, gateway: str):
    """Send test dispatch request to remote server endpoint and display raw response."""
    endpoint = f"{base_url.rstrip('/')}/api/whatsapp/direct-test"
    print(f"\n=======================================================")
    print(f"[*] Testing Remote Server: {endpoint}")
    print(f"[*] Target Recipient:    {to}")
    print(f"[*] Gateway Mode:        {gateway}")
    print(f"[*] Message Content:     {message}")
    print(f"=======================================================\n")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            print(f"[>] Sending HTTP POST to {endpoint}...")
            resp = await client.post(
                endpoint,
                json={"to": to, "message": message, "gateway_type": gateway},
            )
            print(f"[<] HTTP Status: {resp.status_code}")
            try:
                data = resp.json()
                print("\n[ RAW NETWORK RESPONSE FROM SERVER ]")
                print(json.dumps(data, indent=2))
                if data.get("success"):
                    print(f"\n[OK] SUCCESS: WhatsApp Message Dispatched! Message ID: {data.get('message_id')}")
                else:
                    print(f"\n[ERROR] FAILED: {data.get('error_message') or data.get('error')}")
            except Exception:
                print(f"[<] Raw Response Body:\n{resp.text}")
    except httpx.ConnectError as ce:
        print(f"[!] Connection failed to {endpoint}: {ce}")
    except Exception as exc:
        print(f"[!] Unexpected error during remote test: {exc}")


async def run_local_test(to: str, message: str, gateway: str):
    """Directly invoke local Python WhatsApp gateways and print raw network exchange."""
    print(f"\n=======================================================")
    print(f"[*] Direct Local WhatsApp Gateway Verification Test")
    print(f"[*] Target Recipient:    {to}")
    print(f"[*] Gateway Mode:        {gateway}")
    print(f"[*] Message Content:     {message}")
    print(f"=======================================================\n")

    from app.services.whatsapp_service import (
        MetaCloudWhatsAppGateway,
        MockWhatsAppGateway,
        WhatsAppWebGateway,
        get_whatsapp_gateway,
    )

    if gateway == "web":
        print("[1/2] Connecting to local Baileys WebSocket bridge at http://127.0.0.1:3001/status...")
        gw = WhatsAppWebGateway()
        status_check = await gw.test_connection()
        print(f"[*] Bridge Status: {json.dumps(status_check, indent=2)}")

        print(f"\n[2/2] Attempting message dispatch to {to} via Baileys bridge...")
        result = await gw.send_text_message(to_phone=to, message=message)

    elif gateway == "meta":
        print("[1/2] Querying Meta WhatsApp Cloud API credentials...")
        from app.config import settings
        gw = MetaCloudWhatsAppGateway()
        phone_id = settings.META_PHONE_NUMBER_ID or ""
        token = settings.META_WHATSAPP_TOKEN or ""
        print(f"[*] Meta Phone Number ID: {phone_id[:4]}*** (length: {len(phone_id)})")
        print(f"[*] Meta Token Configured: {bool(token)}")

        print(f"\n[2/2] Attempting message dispatch to {to} via Meta Graph API v21.0...")
        result = await gw.send_text_message(
            to_phone=to,
            message=message,
            phone_number_id=phone_id,
            access_token=token,
        )

    elif gateway == "mock":
        print("[*] Running Mock / Dry-Run Gateway...")
        gw = MockWhatsAppGateway()
        result = await gw.send_text_message(to_phone=to, message=message)

    else:
        gw = get_whatsapp_gateway(gateway)
        result = await gw.send_text_message(to_phone=to, message=message)

    print("\n-------------------------------------------------------")
    print("[ RAW RESULT OBJECT ]")
    print(f"  Success:       {result.success}")
    print(f"  Message ID:    {result.message_id}")
    print(f"  Error Message: {result.error_message}")
    print(f"  Raw Response:  {json.dumps(result.raw_response, indent=2) if result.raw_response else 'None'}")
    print("-------------------------------------------------------")

    if result.success:
        print(f"\n[OK] SUCCESS: Real message accepted by WhatsApp server (ID: {result.message_id})")
        sys.exit(0)
    else:
        print(f"\n[ERROR] FAILED: Message was not delivered.")
        print(f"        Reason: {result.error_message}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Test direct WhatsApp message delivery.")
    parser.add_argument("--to", default="+919754119551", help="Recipient WhatsApp number (default: +919754119551)")
    parser.add_argument(
        "--message",
        default="Arovida PET-CT Centre: Live session verification test message.",
        help="Message text to dispatch",
    )
    parser.add_argument(
        "--gateway",
        choices=["web", "meta", "mock", "auto"],
        default="web",
        help="WhatsApp gateway to use (default: web)",
    )
    parser.add_argument(
        "--url",
        default="",
        help="Remote server URL to test (e.g. https://whatsapp-reminder-agent-zybo.onrender.com)",
    )

    args = parser.parse_args()

    if args.url:
        asyncio.run(run_remote_test(args.url, args.to, args.message, args.gateway))
    else:
        asyncio.run(run_local_test(args.to, args.message, args.gateway))


if __name__ == "__main__":
    main()
