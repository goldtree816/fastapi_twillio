# WhatsApp FastAPI Service

A separate microservice to handle Twilio webhooks and APIs, communicating with Odoo via JSON‑RPC.

## Setup
1. Create `.env` with your credentials.
2. Install dependencies: `pip install -r requirements.txt`
3. Run: `uvicorn main:app --host 0.0.0.0 --port 8000 --reload`
4. Expose via ngrok: `ngrok http 8000`
5. Update `WEBHOOK_BASE_URL` in `.env` with the ngrok URL and restart.
6. Set Twilio webhook to `https://<ngrok-url>/webhook/inbound`.