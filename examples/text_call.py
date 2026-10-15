"""Hold a text conversation with a turn service through a running gateway.

The smallest client of the gateway's call-creation API and widget socket: it
creates a text-mode call that points at your turn service, connects the
``ws_url`` the gateway returns, prints every line the agent says, sends the
lines you gave it one per agent turn, and exits 0 when the call has ended.

    pip install websockets
    python examples/text_call.py --say "hello there" --say goodbye

Defaults: the gateway at http://127.0.0.1:8002, the turn service at
http://127.0.0.1:9000/turn/v1 (the echo service), tenant ``demo``. The turn
service address is where the GATEWAY will post turns, so inside Docker it is
the service's name on the compose network (``http://echo:9000/turn/v1``).
Standard library plus ``websockets``; no vogon import.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

from websockets.sync.client import connect

# After the last line is answered, how long to wait for the turn service to
# end the call itself before hanging up from this side.
_END_GRACE_SEC = 2.0


def create_call(gateway: str, tenant: str, turn_url: str, service_key: str) -> dict:
    """POST /v1/tenants/{tenant}/calls: a text-mode call bound to the turn service."""
    body = {
        "mode": "text",
        "app": {"base_url": turn_url, "auth_token": "", "status_webhook": ""},
        "app_payload": {},
    }
    headers = {"content-type": "application/json"}
    if service_key:
        headers["authorization"] = f"Bearer {service_key}"
    request = urllib.request.Request(f"{gateway.rstrip('/')}/v1/tenants/{tenant}/calls",
                                     data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def converse(ws_url: str, lines: list[str], timeout_sec: float) -> int:
    """Drive the widget socket: speak each line after the agent speaks; 0 when the call ended."""
    pending = list(lines)
    deadline = time.monotonic() + timeout_sec
    hung_up = False
    with connect(ws_url, open_timeout=10) as socket:
        while time.monotonic() < deadline:
            wait = _END_GRACE_SEC if (not pending and not hung_up) else max(0.1, deadline - time.monotonic())
            try:
                frame = json.loads(socket.recv(timeout=wait))
            except TimeoutError:
                if pending or hung_up:
                    continue
                socket.send(json.dumps({"type": "client.hangup"}))
                hung_up = True
                continue
            kind = frame.get("type")
            payload = frame.get("payload") or {}
            if kind == "server.message":
                print(f"agent: {payload.get('text', '')}", flush=True)
                if pending:
                    line = pending.pop(0)
                    print(f"you:   {line}", flush=True)
                    socket.send(json.dumps({"type": "client.message", "payload": {"text": line}}))
            elif kind == "server.call_ended":
                print(f"call ended: {payload.get('reason')} after {payload.get('duration_sec')} s", flush=True)
                return 0
            elif kind == "server.error":
                print(f"gateway error: {payload}", file=sys.stderr)
                return 1
    print(f"no end of call within {timeout_sec:.0f} s", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--gateway", default="http://127.0.0.1:8002", help="the gateway's base URL")
    parser.add_argument("--turn", default="http://127.0.0.1:9000/turn/v1",
                        help="the turn service's Turn API root, as the GATEWAY reaches it")
    parser.add_argument("--tenant", default="demo")
    parser.add_argument("--key", default="", help="GATEWAY_SERVICE_KEY, when the gateway has one")
    parser.add_argument("--say", action="append", default=[], metavar="TEXT",
                        help="a line to send, one per agent turn (repeatable)")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for the call to end")
    args = parser.parse_args(argv)
    try:
        created = create_call(args.gateway, args.tenant, args.turn, args.key)
    except urllib.error.URLError as exc:
        print(f"could not create a call on {args.gateway}: {exc}", file=sys.stderr)
        return 1
    print(f"call {created['call_id']} created", flush=True)
    return converse(created["ws_url"], args.say, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
