import httpx
import logging
import re
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

class TwilioError(Exception):
    def __init__(self, message: str, *, code: Optional[str] = None, status_code: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code

def _extract_twilio_error_code(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"\b(\d{5})\b", text)
    return m.group(1) if m else None

def send_whatsapp_message(
    account_sid: str,
    auth_token: str,
    from_number: str,
    to_number: str,
    body: str,
    media_url: Optional[str] = None,
) -> str:
    if not from_number.startswith("whatsapp:"):
        from_number = f"whatsapp:{from_number}"
    if not to_number.startswith("whatsapp:"):
        to_number = f"whatsapp:{to_number}"

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    auth = (account_sid, auth_token)
    data = {"From": from_number, "To": to_number, "Body": body}
    if media_url:
        data["MediaUrl"] = media_url

    try:
        with httpx.Client() as client:
            resp = client.post(url, data=data, auth=auth, timeout=15)
            if resp.status_code in (200, 201):
                return resp.json().get("sid")
            resp_text = resp.text or ""
            code = _extract_twilio_error_code(resp_text)
            error_msg = f"Twilio error {resp.status_code} (code={code}): {resp_text}"
            logger.error(error_msg)
            raise TwilioError(error_msg, code=code, status_code=resp.status_code)
    except TwilioError:
        raise
    except Exception as e:
        logger.error("Twilio send exception: %s", e)
        raise TwilioError(f"Twilio send exception: {e}")

def set_webhook(account_sid: str, auth_token: str, number_sid: str, webhook_url: str) -> bool:
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/IncomingPhoneNumbers/{number_sid}.json"
    auth = (account_sid, auth_token)
    data = {"SmsUrl": webhook_url, "SmsMethod": "POST"}
    try:
        with httpx.Client() as client:
            resp = client.post(url, data=data, auth=auth, timeout=15)
            if resp.status_code in (200, 201):
                logger.info("Webhook set for number %s to %s", number_sid, webhook_url)
                return True
            logger.error("Failed to set webhook: %s", resp.text)
            return False
    except Exception as e:
        logger.error("Webhook update exception: %s", e)
        return False

def buy_number(account_sid: str, auth_token: str, phone_number: str,
               friendly_name: Optional[str] = None) -> Dict[str, Any]:
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/IncomingPhoneNumbers.json"
    auth = (account_sid, auth_token)
    data = {"PhoneNumber": phone_number, "FriendlyName": friendly_name or phone_number}
    try:
        with httpx.Client() as client:
            resp = client.post(url, data=data, auth=auth, timeout=30)
            if resp.status_code in (200, 201):
                return resp.json()
            raise Exception(f"Twilio purchase error {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error("Twilio purchase exception: %s", e)
        raise

def make_voice_call(
    account_sid: str,
    auth_token: str,
    from_number: str,
    to_number: str,
    twiml: str,
    status_callback_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Initiate an outgoing voice call with custom TwiML."""
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json"
    auth = (account_sid, auth_token)
    data = {"From": from_number, "To": to_number, "Twiml": twiml}
    if status_callback_url:
        data["StatusCallback"] = status_callback_url
        data["StatusCallbackMethod"] = "POST"
        data["StatusCallbackEvent"] = ["initiated", "ringing", "answered", "completed"]
    try:
        with httpx.Client() as client:
            resp = client.post(url, data=data, auth=auth, timeout=30)
            if resp.status_code in (200, 201):
                return resp.json()
            raise Exception(f"Twilio voice call error {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error("Twilio voice call exception: %s", e)
        raise
