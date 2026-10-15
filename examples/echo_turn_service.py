"""The smallest turn service vogon can drive — a reference implementation of the
Turn API (le-vogon-contract) in about a hundred lines.

It greets the caller, repeats back every final utterance, and ends the call when
the caller says goodbye. No model, no state beyond a per-call turn counter.

    pip install le-vogon-contract fastapi uvicorn
    python examples/echo_turn_service.py                 # serves /turn/v1 on 127.0.0.1:9000

ECHO_HOST / ECHO_PORT change the bind address (the Docker image runs it on
0.0.0.0:9000). Then create a call on the gateway pointing ``app.base_url`` at
``http://127.0.0.1:9000/turn/v1`` (GETTING_STARTED.md), or hold a text
conversation with ``examples/text_call.py``.
"""
from __future__ import annotations

import os
import uuid

import uvicorn
from fastapi import FastAPI, Request

from vogon_contract import (
    CallConfig,
    EndCallResponse,
    ProcessEventsResponse,
    RecordTransferResponse,
    SilenceThreshold,
    SpeakCommand,
    StartCallResponse,
    UtteranceFinal,
)
from vogon_contract.codec import (
    end_call_request_from_wire,
    process_events_request_from_wire,
    record_transfer_request_from_wire,
    start_call_request_from_wire,
    to_wire,
)

app = FastAPI(title="echo turn service")
_turns: dict[str, int] = {}


@app.get("/health")
async def health():
    """Liveness for docker-compose.yml's healthcheck (the Turn API itself has no such endpoint)."""
    return {"ok": True, "service": "echo-turn-service", "calls": len(_turns)}


def _speak(text: str) -> SpeakCommand:
    return SpeakCommand(audio_id=f"echo-{uuid.uuid4().hex[:8]}", text=text, agent_id="echo")


@app.post("/turn/v1/start_call")
async def start_call(request: Request):
    req = start_call_request_from_wire(await request.json())
    _turns[req.call_id] = 0
    # CallConfig is the gateway's per-call policy: silence clock, barge-in,
    # recording, fillers. The defaults are sensible; a real service tunes them.
    # persona_voice is the one required field: the TTS voice for spoken modes
    # (a Deepgram aura-2 voice name; unused on a text call).
    return to_wire(StartCallResponse(
        call_config=CallConfig(persona_voice="aura-2-thalia-en", persona_name="Echo", workflow_id="echo"),
        speak=[_speak("Hi, this is the echo service. Say anything and I will say it back. "
                      "Say goodbye when you are done.")]))


@app.post("/turn/v1/process_events")
async def process_events(request: Request):
    req = process_events_request_from_wire(await request.json())
    speak: list[SpeakCommand] = []
    for event in req.events:
        if isinstance(event, UtteranceFinal):
            _turns[req.call_id] = _turns.get(req.call_id, 0) + 1
            if "goodbye" in event.text.lower():
                return to_wire(ProcessEventsResponse(
                    speak=[_speak("Goodbye.")], action="end_call", end_reason="caller_farewell"))
            speak.append(_speak(f"You said: {event.text}"))
        elif isinstance(event, SilenceThreshold) and event.threshold_id == "alert":
            speak.append(_speak("Are you still there?"))
        elif isinstance(event, SilenceThreshold) and event.threshold_id == "exit":
            return to_wire(ProcessEventsResponse(
                speak=[_speak("I will let you go. Goodbye.")], action="end_call", end_reason="silence_exit"))
        # Every other event (eager utterances, delivery outcomes, line verdicts,
        # playout marks) is a physical fact this service has no use for.
    return to_wire(ProcessEventsResponse(speak=speak))


@app.post("/turn/v1/record_transfer")
async def record_transfer(request: Request):
    record_transfer_request_from_wire(await request.json())
    return to_wire(RecordTransferResponse(appended=0))


@app.post("/turn/v1/end_call")
async def end_call(request: Request):
    req = end_call_request_from_wire(await request.json())
    turns = _turns.pop(req.call_id, 0)
    print(f"call {req.call_id} ended ({req.reason}) after {turns} turn(s)")
    return to_wire(EndCallResponse())


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("ECHO_HOST", "127.0.0.1"), port=int(os.environ.get("ECHO_PORT", "9000")))
