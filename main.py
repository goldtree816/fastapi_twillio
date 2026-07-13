from fastapi import FastAPI, Request, Response, Form, HTTPException
import logging
import httpx
from typing import Optional
from datetime import datetime

from config import (
    ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD,
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
    WEBHOOK_BASE_URL
)
from odoo_client import OdooClient
from twilio_client import send_whatsapp_message, set_webhook, buy_number, TwilioError

from models import SendMessageRequest, BuyNumberRequest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="WhatsApp FastAPI Service", version="1.0")

odoo = OdooClient(ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD)

def ensure_odoo():
    if not odoo.uid:
        if not odoo.login():
            logger.error("Odoo login failed. Check credentials and Odoo server.")
            raise HTTPException(status_code=500, detail="Could not authenticate with Odoo. Check credentials.")
    return True

def now_iso():
    return datetime.now().isoformat()

# ---------- Incoming webhook ----------
@app.post("/webhook/inbound")
async def twilio_webhook(
    From: str = Form(...),
    Body: Optional[str] = Form(None),
    MessageSid: Optional[str] = Form(None),
    MediaUrl0: Optional[str] = Form(None),
):
    phone = From.replace("whatsapp:", "").strip()
    body = Body or ""
    ensure_odoo()

    thread_ids = odoo.call("whatsapp.thread", "search", [[["phone", "=", phone]]])
    if thread_ids:
        thread_id = thread_ids[0]
        current = odoo.call("whatsapp.thread", "read", [[thread_id], ["unread_count"]])
        unread = current[0]["unread_count"] + 1 if current else 1
        odoo.call("whatsapp.thread", "write", [[thread_id], {
            "last_message": body[:200],
            "last_message_date": now_iso(),
            "unread_count": unread,
            "status": "online",
        }])
    else:
        thread_id = odoo.call("whatsapp.thread", "create", [{
            "name": phone,
            "phone": phone,
            "avatar_color": "#25D366",
            "status": "online",
            "thread_type": "external",
            "last_message": body[:200],
            "last_message_date": now_iso(),
            "unread_count": 1,
        }])

    odoo.call("whatsapp.message", "create", [{
        "thread_id": thread_id,
        "body": body,
        "direction": "incoming",
        "message_type": "external",
        "status": "delivered",
        "timestamp": now_iso(),
        "twilio_sid": MessageSid,
    }])

    logger.info("Incoming message from %s saved to thread %s", phone, thread_id)
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        media_type="text/xml"
    )

# ---------- Send message ----------
@app.post("/send_message")
async def send_message(req: SendMessageRequest):
    ensure_odoo()

    numbers = odoo.call("whatsapp.purchased_number", "search_read",
                        [[["is_sending_number", "=", True], ["status", "=", "active"]], ["number"]])
    if not numbers:
        numbers = odoo.call("whatsapp.purchased_number", "search_read",
                            [[["status", "=", "active"]], ["number"]], limit=1)
    if not numbers:
        raise HTTPException(status_code=400, detail="No active WhatsApp number found in Odoo.")

    # HARDCODE FOR TESTING – REMOVE AFTER
    from_number = "+14155238886"

    try:
        sid = send_whatsapp_message(
            TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
            from_number, req.to_phone, req.body, req.media_url
        )
        return {"status": "success", "twilio_sid": sid}
    except Exception as e:
        error_str = str(e)
        logger.error("Twilio send error: %s", error_str)
        raise HTTPException(status_code=500, detail=f"Twilio send failed: {error_str}")



# ---------- Available numbers ----------
@app.post("/available_numbers")
async def available_numbers(request: Request):
    # ... (unchanged – keep as before)
    try:
        body = await request.json()
        country_code = body.get("country_code", "US")
        number_type = body.get("number_type", "local").capitalize()
        limit = body.get("limit", 20)
    except Exception:
        country_code = "US"
        number_type = "Local"
        limit = 20

    account_sid = TWILIO_ACCOUNT_SID
    auth_token = TWILIO_AUTH_TOKEN

    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"
        f"/AvailablePhoneNumbers/{country_code}/{number_type}.json"
    )

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                auth=(account_sid, auth_token),
                params={"Limit": limit},
                timeout=15
            )
            if resp.status_code == 200:
                data = resp.json()
                numbers = []
                for num in data.get("available_phone_numbers", []):
                    caps = [k.title() for k, v in num.get("capabilities", {}).items() if v]
                    numbers.append({
                        "id": num.get("phone_number"),
                        "number": num.get("phone_number"),
                        "display_number": num.get("friendly_name"),
                        "type": number_type,
                        "capabilities": ", ".join(caps),
                        "monthlyCost": str(num.get("monthly_price", "0.00")),
                        "setupFee": str(num.get("setup_fee", "0.00")),
                    })
                return {"status": "success", "numbers": numbers}
            else:
                error_msg = f"Twilio API error {resp.status_code}: {resp.text}"
                logger.error(error_msg)
                return {"status": "error", "message": error_msg}
    except Exception as e:
        logger.error("Available numbers error: %s", e)
        return {"status": "error", "message": str(e)}

# ---------- Buy number ----------
@app.post("/buy_number")
async def purchase_number(req: BuyNumberRequest):
    ensure_odoo()
    try:
        result = buy_number(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
                            req.phone_number, req.friendly_name)
        number_sid = result["sid"]
        purchased_number = result["phone_number"]

        webhook_url = f"{WEBHOOK_BASE_URL}/webhook/inbound"
        webhook_ok = set_webhook(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
                                 number_sid, webhook_url)

        odoo.call("whatsapp.purchased_number", "create", [{
            "number": purchased_number,
            "sid": number_sid,
            "friendly_name": req.friendly_name or purchased_number,
            "status": "active",
            "purchase_date": now_iso(),
            "is_sending_number": True,
        }])

        logger.info("Number %s purchased and webhook set", purchased_number)
        return {
            "status": "success",
            "number": purchased_number,
            "sid": number_sid,
            "webhook_set": webhook_ok
        }
    except Exception as e:
        logger.error("Purchase failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

# ---------- List active numbers ----------
@app.get("/numbers")
async def list_numbers():
    ensure_odoo()
    numbers = odoo.call("whatsapp.purchased_number", "search_read",
                        [[["status", "=", "active"]], ["number", "sid", "friendly_name", "is_sending_number"]])
    return {"numbers": numbers}