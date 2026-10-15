"""The voice gateway for the Twilio-mode smokes, with the REST dial faked.

Same process as run.sh (uvicorn on vogon.gateway_service.main:app), except
`_place_twilio_call` returns a CallSid without dialling anyone, so a test
client can connect to /ws/twilio with that CallSid in its `start` frame and
speak the media-stream protocol itself. Nothing else is patched: the
transport, floor controller, STT, TTS and recorder are the production ones.
Started by tests/smoke_twilio_floor.py on GATEWAY_PORT."""
import os
import sys
import uuid
from pathlib import Path  # noqa: E402

GW = str(Path(__file__).resolve().parents[1])
DP = str(Path(GW).parent / "data-plane")
CONTRACT = str(Path(GW).parent / "vogon-contract" / "src")
for d in (DP, GW, CONTRACT):
    if d not in sys.path:
        sys.path.insert(0, d)

import uvicorn  # noqa: E402

from vogon.gateway_service import routes  # noqa: E402


async def _fake_place_twilio_call(dial, public_base, token) -> str:
    sid = f"CAfake{uuid.uuid4().hex[:24]}"
    routes.logger.info("FAKE Twilio dial | to=%s | call_sid=%s (no call placed)", dial.to, sid)
    return sid


routes._place_twilio_call = _fake_place_twilio_call

from vogon.gateway_service.main import app  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("GATEWAY_PORT", "8802")),
                log_level="info")
