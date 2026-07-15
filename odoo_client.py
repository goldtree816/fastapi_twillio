import httpx
import logging
from typing import Any, List, Dict, Optional

logger = logging.getLogger(__name__)

class OdooClient:
    def __init__(self, url: str, db: str, username: str, password: str):
        self.url = url.rstrip('/')
        self.db = db
        self.username = username
        self.password = password
        self.uid = None
        self.cookies = None

    def login(self) -> bool:
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"db": self.db, "login": self.username, "password": self.password}
        }
        try:
            with httpx.Client() as client:
                resp = client.post(f"{self.url}/web/session/authenticate", json=payload, timeout=10)
                if resp.status_code != 200:
                    logger.error("Odoo login HTTP error: %s", resp.text)
                    return False
                result = resp.json().get("result", {})
                self.uid = result.get("uid")
                self.cookies = resp.cookies
                logger.info("Odoo login successful, uid=%s", self.uid)
                return self.uid is not None
        except Exception as e:
            logger.error("Odoo login exception: %s", e)
            return False

    def call(self, model: str, method: str, args: List[Any] = None, kwargs: Dict[str, Any] = None) -> Any:
        if not self.uid:
            raise Exception("Not authenticated. Call login() first.")
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"model": model, "method": method, "args": args or [], "kwargs": kwargs or {}}
        }
        try:
            with httpx.Client() as client:
                resp = client.post(
                    f"{self.url}/web/dataset/call_kw",
                    json=payload,
                    cookies=self.cookies,
                    timeout=15
                )
                if resp.status_code != 200:
                    raise Exception(f"Odoo API HTTP error {resp.status_code}: {resp.text}")
                result = resp.json().get("result")
                logger.debug("Odoo call %s.%s -> %s", model, method, result)
                return result
        except Exception as e:
            logger.error("Odoo call failed: %s", e)
            raise
