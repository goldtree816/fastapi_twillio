# TODO

- [ ] Update `twilio_client.py` to introduce `TwilioError` and raise it from `send_whatsapp_message` with parsed Twilio error code.
- [ ] Update root `main.py` `/send_message` route to catch `TwilioError` and map known codes (21404, 21660) to specific HTTP responses.
- [x] Run `python -m py_compile` for the edited files to ensure syntax is valid.


