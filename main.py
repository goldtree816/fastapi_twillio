from fastapi import FastAPI, Request, Response, Form, HTTPException, Depends, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from datetime import datetime, timedelta
from jose import JWTError, jwt
from typing import Optional
import logging
import httpx
import smtplib
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from config import (
    ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD,
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
    WEBHOOK_BASE_URL,
    JWT_SECRET_KEY, JWT_ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES,
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM,
    FRONTEND_BASE_URL, RESET_TOKEN_EXPIRE_MINUTES
)
from odoo_client import OdooClient
from twilio_client import send_whatsapp_message, set_webhook, buy_number, make_voice_call, TwilioError
from models import SendMessageRequest, BuyNumberRequest, MakeCallRequest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="WhatsApp FastAPI Service", version="1.0")

# System Odoo client
odoo = OdooClient(ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

# ---------- Helper functions ----------
def ensure_odoo():
    if not odoo.uid:
        if not odoo.login():
            logger.error("Odoo login failed. Check credentials and Odoo server.")
            raise HTTPException(status_code=500, detail="Could not authenticate with Odoo. Check credentials.")
    return True

def now_iso():
    return datetime.now().isoformat()

def authenticate_user(username: str, password: str):
    user_client = OdooClient(ODOO_URL, ODOO_DB, username, password)
    if not user_client.login():
        return None
    try:
        user_data = user_client.call("res.users", "read", [[user_client.uid], ["name", "login", "email"]])
        if user_data and len(user_data) > 0:
            return {
                "uid": user_client.uid,
                "login": username,
                "name": user_data[0].get("name", username),
                "email": user_data[0].get("email", "")
            }
    except Exception as e:
        logger.error("Failed to fetch user data: %s", e)
    return {"uid": user_client.uid, "login": username, "name": username, "email": ""}

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
    return encoded_jwt

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        return {"username": username, "uid": payload.get("uid"), "name": payload.get("name")}
    except JWTError:
        raise credentials_exception

# ---------- Email helper ----------
def send_email(to_email: str, subject: str, body: str):
    if not SMTP_HOST or not SMTP_USER:
        logger.warning("SMTP not configured; email not sent.")
        return False
    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_FROM
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, to_email, msg.as_string())
        logger.info("Email sent to %s", to_email)
        return True
    except Exception as e:
        logger.error("Failed to send email: %s", e)
        return False

def get_user_by_email_or_login(login_or_email: str):
    ensure_odoo()
    domain = ['|', ('login', '=', login_or_email), ('email', '=', login_or_email)]
    users = odoo.call(
        "res.users",
        "search_read",
        args=[domain, ['id', 'login', 'email', 'name']],
        kwargs={'context': {'active_test': False}}
    )
    logger.info("Search for %s returned: %s", login_or_email, users)
    if users:
        return users[0]
    return None

# ---------- Login ----------
@app.post("/login", response_model=dict)
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user["login"], "uid": user["uid"], "name": user["name"]},
        expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}

# ---------- Forgot password ----------
@app.post("/forgot-password")
async def forgot_password(email: str = Form(...)):
    user = get_user_by_email_or_login(email)
    if not user:
        logger.info("Password reset requested for non-existent user: %s", email)
        return {"message": "If your email is registered, you will receive a reset link."}
    reset_expires = timedelta(minutes=RESET_TOKEN_EXPIRE_MINUTES)
    token_data = {
        "sub": user["login"],
        "uid": user["id"],
        "email": user["email"],
        "purpose": "password_reset"
    }
    reset_token = create_access_token(token_data, expires_delta=reset_expires)
    logger.info("Reset token: %s", reset_token)
    reset_link = f"{FRONTEND_BASE_URL}/reset-password?token={reset_token}"
    email_body = f"""
    Hi {user.get('name', user['login'])},

    You requested to reset your password for the WhatsApp Dashboard.
    Click the link below to set a new password:

    {reset_link}

    This link will expire in {RESET_TOKEN_EXPIRE_MINUTES} minutes.

    If you did not request this, please ignore this email.
    """
    success = send_email(user["email"], "Password Reset Request", email_body)
    if success:
        return {"message": "Password reset email sent."}
    else:
        raise HTTPException(status_code=500, detail="Failed to send email. Please try again later.")

# ---------- Reset password ----------
@app.post("/reset-password")
async def reset_password(token: str = Form(...), new_password: str = Form(...)):
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token.")
    if payload.get("purpose") != "password_reset":
        raise HTTPException(status_code=400, detail="Invalid token purpose.")
    uid = payload.get("uid")
    if not uid:
        raise HTTPException(status_code=400, detail="Invalid token payload.")
    ensure_odoo()
    try:
        odoo.call("res.users", "write", [[uid], {"password": new_password}])
        logger.info("Password reset for user %s (uid %s)", payload.get("sub"), uid)
        return {"message": "Password updated successfully."}
    except Exception as e:
        logger.error("Failed to update password: %s", e)
        raise HTTPException(status_code=500, detail="Could not update password. Please try again.")

# ---------- Public webhook (no auth) ----------
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

# ---------- Protected WhatsApp endpoints ----------
@app.post("/send_message")
async def send_message(req: SendMessageRequest, current_user: dict = Depends(get_current_user)):
    ensure_odoo()
    numbers = odoo.call("whatsapp.purchased_number", "search_read",
                        [[["is_sending_number", "=", True], ["status", "=", "active"]], ["number"]])
    if not numbers:
        numbers = odoo.call("whatsapp.purchased_number", "search_read",
                            [[["status", "=", "active"]], ["number"]], limit=1)
    if not numbers:
        raise HTTPException(status_code=400, detail="No active WhatsApp number found in Odoo.")
    from_number = numbers[0]["number"]
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

@app.post("/available_numbers")
async def available_numbers(request: Request, current_user: dict = Depends(get_current_user)):
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

@app.post("/buy_number")
async def purchase_number(req: BuyNumberRequest, current_user: dict = Depends(get_current_user)):
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

@app.get("/numbers")
async def list_numbers(current_user: dict = Depends(get_current_user)):
    ensure_odoo()
    numbers = odoo.call("whatsapp.purchased_number", "search_read",
                        [[["status", "=", "active"]], ["number", "sid", "friendly_name", "is_sending_number"]])
    return {"numbers": numbers}

# ---------- Voice call with interactive IVR ----------
@app.post("/make_call")
async def make_call(req: MakeCallRequest, current_user: dict = Depends(get_current_user)):
    """
    Initiate a voice call with a two‑way conversation (speech + DTMF).
    The caller can speak or press a key, and the system responds.
    """
    ensure_odoo()
    # Fetch the active Twilio number (must be voice-enabled)
    numbers = odoo.call("whatsapp.purchased_number", "search_read",
                        [[["is_sending_number", "=", True], ["status", "=", "active"]], ["number"]])
    if not numbers:
        # Fallback: pick any active number
        numbers = odoo.call("whatsapp.purchased_number", "search_read",
                            [[["status", "=", "active"]], ["number"]], limit=1)
    if not numbers:
        raise HTTPException(status_code=400, detail="No active Twilio number found in Odoo.")
    from_number = numbers[0]["number"]

    # Build interactive TwiML with <Gather> (speech + DTMF)
    action_url = f"{WEBHOOK_BASE_URL}/voice/response"
    twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Say>Hello, this is a two‑way conversation test. Please speak your name or press any key.</Say>
        <Gather input="speech dtmf" action="{action_url}" method="POST" timeout="5" numDigits="1">
            <Say>We didn't hear anything. Please try again.</Say>
        </Gather>
    </Response>'''

    try:
        result = make_voice_call(
            TWILIO_ACCOUNT_SID,
            TWILIO_AUTH_TOKEN,
            from_number,
            req.to_phone,
            twiml=twiml,
            status_callback_url=req.status_callback_url
        )
        call_sid = result.get("sid")
        logger.info("Voice call initiated: %s to %s", call_sid, req.to_phone)
        return {"status": "success", "call_sid": call_sid, "call_data": result}
    except Exception as e:
        logger.error("Voice call error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/voice/response")
async def voice_response(request: Request):
    """
    Handle the user's speech or DTMF input and respond.
    Public endpoint – called by Twilio.
    """
    form = await request.form()
    speech_result = form.get("SpeechResult")
    digits = form.get("Digits")
    logger.info("Voice response: speech='%s', digits='%s'", speech_result, digits)

    if speech_result:
        reply = f"You said: {speech_result}"
    elif digits:
        reply = f"You pressed: {digits}"
    else:
        reply = "No input received."

    twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Say>{reply}</Say>
    </Response>'''
    return Response(content=twiml, media_type="text/xml")
