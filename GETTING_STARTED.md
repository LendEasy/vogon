# Getting started

In about ten minutes you will have a gateway running, a turn service behind
it, and a conversation held through the two: the gateway creates the call,
your service decides what to say, the gateway delivers it. The conversation is
typed rather than spoken, so this needs **no API keys and no audio hardware**;
the last section says what to add for a browser or phone call.

The turn service is `examples/echo_turn_service.py`, a complete implementation
of the Turn API in about a hundred lines: it greets the caller, repeats back
every line, and ends the call on "goodbye". The client is
`examples/text_call.py`, which creates a text-mode call on the gateway and
holds the conversation over the call's WebSocket.

Pick one track. Both end at the same conversation.

```
  text_call.py ──POST /v1/tenants/demo/calls──▶  vogon  ──POST /turn/v1/*──▶  echo_turn_service.py
               ◀──ws: server.message───         ◀── SpeakCommand[] ──
```

Every step below is exercised by `tests/test_getting_started.py`; the table at
the end says which test checks which step.

## The Python track

Needs Python 3.10 to 3.12 and a checkout with `voice-gateway/` and
`vogon-contract/` side by side. The Turn API package is installed from the
sibling here so that the two stay in step; a turn service of your own takes
it from PyPI as `le-vogon-contract`.

**1. Install.** From `voice-gateway/`:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ../vogon-contract
pip install -e ".[front,dev]"
```

`./install.sh` does the same and also builds the echo canceller, which a text
call does not need. If you skip it, step 2 tells the gateway so.

**2. Configure.** Copy the template and set two values for a key-less run:

```bash
cp env.example .env
```

In `.env`, add `GATEWAY_CAPABILITY_POLICY=warn` (the gateway otherwise refuses
to start without the native audio libraries, which is the right default for a
phone deployment) and `REDIS_ENABLED=false` (no session store beyond this
process). Leave `DEEPGRAM_API_KEY` empty; nothing in this track speaks.

**3. Start the turn service**, in its own terminal:

```bash
python examples/echo_turn_service.py
```

It serves `/turn/v1` on `127.0.0.1:9000`. `ECHO_HOST` and `ECHO_PORT` move it.

**4. Start the gateway**, in a second terminal, and check it is up:

```bash
vogon                                  # or: ./run.sh
curl -s localhost:8002/health
```

The health body lists the native capabilities; `false` entries are fine here.

```json
{"ok": true, "service": "voice-gateway", "capabilities": {"aec": true, "vad": true, "audioop": true}}
```

**5. Talk.** In a third terminal:

```bash
python examples/text_call.py --say "hello there" --say goodbye
```

The script creates the call, pointing the gateway at the echo service, and
prints both sides:

```
call gwc-… created
agent: Hi, this is the echo service. Say anything and I will say it back. Say goodbye when you are done.
you:   hello there
agent: You said: hello there
you:   goodbye
agent: Goodbye.
call ended: caller_farewell after 0.1 s
```

It exits 0 when the call has ended. The echo service's terminal prints
`call gwc-… ended (caller_farewell) after 2 turn(s)`.

## The Docker track

Needs Docker with Compose v2. The image is built from this directory alone:
the Turn API package is installed from its repository (or from PyPI once
released), or from a wheel you drop into `wheels/` when you are working on
the two trees together (`python -m build ../vogon-contract -o wheels`).

**1. Build and start** both containers from `voice-gateway/`:

```bash
docker compose up --build --wait
```

The first build takes a few minutes: it compiles the SpeexDSP echo canceller,
so this image can take phone calls as it is. `vogon` listens on `:8002`;
`echo` runs the reference turn service on the compose network, where the
gateway reaches it as `http://echo:9000/turn/v1`. `GATEWAY_PORT` changes the
published port.

**2. Check** the gateway:

```bash
curl -s localhost:8002/health
```

All three capabilities report `true` from the image.

**3. Talk**, from the host. The client needs only the `websockets` package;
the turn-service address is the one the *gateway* uses, so it is the compose
service name:

```bash
pip install websockets
python examples/text_call.py --turn http://echo:9000/turn/v1 --say "hello there" --say goodbye
```

Same conversation as above. `docker compose logs echo` shows the turn
service's side.

**4. Stop** and remove the volume that held recordings and metrics:

```bash
docker compose down -v
```

## What just happened

`text_call.py` made one HTTP request to the gateway, `POST
/v1/tenants/demo/calls`, naming the turn service's address and the mode `text`. The
gateway stored the call, minted a session token, and returned a `ws_url`. On
that socket the gateway sent `server.session_ready`, then called your turn
service's `start_call`, whose reply it delivered as `server.message`. Each
`client.message` you sent became an `UtteranceFinal` event in a
`process_events` request; the echo service's `SpeakCommand`s came back as
`server.message` frames, and its `end_call` action closed the call, which the
gateway reported as `server.call_ended` before telling the turn service
`end_call`. A spoken call differs only in the transport: speech recognition
produces the utterances and synthesis plays the commands.

## Next

- **A browser call.** Create the call with `"mode": "widget"` and connect the
  embeddable widget to the `ws_url`; the gateway needs `DEEPGRAM_API_KEY` for
  speech in and out.
- **A phone call.** `"mode": "twilio"` with a `"twilio": {"to": "+1…"}` block,
  Twilio credentials in `.env`, a public HTTPS `GATEWAY_PUBLIC_BASE_URL` for
  Twilio's callbacks, and the echo canceller built (the Docker image has it;
  for the Python track run `./install.sh` and drop the `warn` policy).
  `DEPLOY.md` covers the rest.
- **Your own turn service.** Four endpoints, described in `README.md` under
  "Writing a turn service"; `examples/echo_turn_service.py` is the template.
  `CallConfig` is where your service sets the silence clock, the barge policy,
  the filler pool and recording for each call.
- **The filler hold and voicemail detection** need `OPEN_ROUTER_API_KEY`; the
  semantic barge judge needs `CEREBRAS_API_KEY`. `CONFIGURATION.md` lists every
  setting.

## How this guide is tested

`tests/test_getting_started.py` runs with the rest of the suite
(`python -m pytest tests -q`). The Docker build and run are gated: set
`VOGON_DOCKER_TESTS=1` to include them.

| step | test |
|---|---|
| every file, command and setting this guide names exists | `test_the_guide_names_only_things_that_exist` |
| Python 2 — `env.example` is a valid starting `.env` | `test_env_example_loads_as_settings` |
| Python 3 — the echo service serves the four Turn API endpoints | `test_the_echo_service_implements_the_turn_api` |
| Python 4 — `/health` reports the capabilities | `test_health_reports_the_capabilities` |
| Python 5 — `text_call.py` holds the conversation through a running gateway | `test_the_text_call_runs_through_a_live_gateway_and_echo_service` |
| Docker — the compose file and Dockerfile agree with each other | `test_compose_and_dockerfile_agree`, `test_compose_config_is_valid` |
| Docker 1–4 — build, health, conversation, teardown | `test_the_docker_track_end_to_end` (gated) |
