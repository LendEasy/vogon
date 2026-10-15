# Voice Gateway — Non-Python Dependencies

M1 item 11 (plan §5). What the gateway half needs beyond `pip install`,
what happens when it's missing, and which env vars feed it. No Docker
images yet — this file is the record a future image build follows.
Startup enforcement: `capabilities.py` (policy `GATEWAY_CAPABILITY_POLICY`,
default `refuse` — see D5).

## 1. Native / system-level

| Dependency | Needed for | System packages (Debian) | If missing |
|---|---|---|---|
| **SpeexDSP AEC** — `speexdsp @ git+https://github.com/xiongyihui/speexdsp-python.git` (SWIG/C++ build) | Echo cancellation (`vogon/gateway/transport/aec.py`) | `git g++ swig libspeex-dev libspeexdsp-dev` (confirmed in `docs/aec-installation.md`) | Silently degrades to **no AEC** — callers hear echo. The capability check makes this loud/fatal. |
| **webrtcvad** (C extension; upstream unmaintained — `webrtcvad-wheels` is the maintained fork) | Speech-gated barge confirm; audio-timeline VAD lane | build tools if no wheel for the target Python | Silently degrades to energy-only barge gate. Capability check makes it loud. |
| **stdlib `audioop`** | ~20 call sites — the entire mulaw↔linear16 transcode path, RMS energy gates, resampling | none — **but removed from Python 3.13** (PEP 594) | Hard break. Pin Python ≤ 3.12 or install `audioop-lts` (`audioop-lts; python_version >= "3.13"`). |
| **numpy** | GCC-PHAT delay estimation in AEC alignment (not the per-frame hot path) | wheel | AEC alignment degrades. |

**Deliberately absent:** no ffmpeg, no sox, no subprocess audio tools
anywhere in the gateway half. All transcoding is in-process `audioop`.

## 2. External services

| Service | Use | Protocol / notes | Env |
|---|---|---|---|
| **Deepgram STT** | Flux v2 listen, streaming; phone audio passes through as mulaw 8 kHz (no transcode) | WS via `deepgram-sdk==6.0.1` | `DEEPGRAM_API_KEY`, `DEEPGRAM_STT_MODEL`, EOT tuning vars |
| **Deepgram TTS** | aura-2; ONE persistent WS per call, reused across turns | WS, same SDK; phone override mulaw/8 kHz | `DEEPGRAM_TTS_MODEL` |
| **Twilio media streams** | Inbound/outbound call audio: base64 **mulaw 8 kHz, 20 ms frames**, raw JSON frames — no SDK on the media path | WS (the gateway serves `/ws/twilio`) | `GATEWAY_PUBLIC_BASE_URL` must be reachable by Twilio (webhooks + stream URL) |
| **Twilio REST** | The dial, warm transfer, recording flags | `twilio>=9.0.0` | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` |
| **Cerebras** | The semantic barge judge ONLY (decision D1 — gateway-owned client in `barge_judge.py`; time-capped, absorb on failure) | HTTPS via `cerebras-cloud-sdk` | `CEREBRAS_API_KEY`, `CEREBRAS_MODEL` |
| **AWS S3** | Recording/artifact upload (optional) | `boto3`, standard credential chain | `S3_ENABLED`, `S3_BUCKET`, `S3_REGION` |
| **Redis** | The session store when `GATEWAY_SESSION_STORE=redis` (the gateway's ONE persistence dependency; refuses to boot redis-less in that mode) | `redis[hiredis]` | `REDIS_URL` |
| **The application (turn service)** | Every turn: `POST {app.base_url}/{start_call,process_events,end_call}` — the binding arrives PER CALL on `/v1/tenants/{tenant}/calls` | httpx | per-call `app.auth_token` |

Not used by the gateway: the control plane (application-side only), the
application DB (none — no sqlalchemy/psycopg2 needed), torch/transformers
(the big image win at M3 packaging).

## 3. Python package set (gateway half)

```
fastapi  uvicorn[standard]  websockets  pydantic  pydantic-settings
python-dotenv  python-multipart  httpx  deepgram-sdk==6.0.1  twilio>=9.0.0
speexdsp @ git+https://github.com/xiongyihui/speexdsp-python.git
webrtcvad>=2.0.10  numpy  aiofiles  boto3>=1.34  redis[hiredis]>=5.0
cerebras-cloud-sdk
audioop-lts; python_version >= "3.13"
```

`requirements.txt` at the root of this tree pins the same set for a plain
`pip install -r`; `pyproject.toml` is the source of truth.

## 4. Static assets

None committed. Ambience synthesizes room-tone/typing when
`vogon/gateway/transport/ambience/assets/*.wav` are absent; the assets README
documents authoring specs. Filler audio is text through the normal TTS
path, never pre-rendered.

## 5. Boot-time enforcement

`GATEWAY_CAPABILITY_POLICY=refuse` (default): a missing AEC/VAD/audioop
aborts startup with the exact remedy in the error. `warn`: logs one ERROR
line per missing capability and continues — for dev boxes and text-only
deployments. `/health` reports per-capability status either way.
