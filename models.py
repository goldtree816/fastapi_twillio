from pydantic import BaseModel
from typing import Optional

class SendMessageRequest(BaseModel):
    to_phone: str
    body: str
    media_url: Optional[str] = None

class BuyNumberRequest(BaseModel):
    phone_number: str
    friendly_name: Optional[str] = None