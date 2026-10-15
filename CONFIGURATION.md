# Configuration

Every setting is an environment variable (upper-case of the name below) or a line in a
`.env` file in the gateway's working directory. Defaults are the values a fresh install
runs with. Timing constants that are deliberately **not** tunable are documented in
[docs/voice-timing-constants.md](docs/voice-timing-constants.md).

Two groups are read by different objects:

- **`GATEWAY_*`** — deployment config of the service process (`vogon/gateway_service/config.py`):
  `GATEWAY_SERVICE_KEY` (the bearer token the application presents), `GATEWAY_PUBLIC_BASE_URL`
  (webhooks and the `ws_url` host), `GATEWAY_SESSION_STORE` (`memory` | `redis`),
  `GATEWAY_TENANT_PROFILES` (a JSON file of per-tenant overrides), `GATEWAY_CAPABILITY_POLICY`
  (`refuse` | `warn`: what to do when the echo canceller or VAD is missing),
  `GATEWAY_LOG_LEVEL`, `GATEWAY_ESCALATION_SAY_VOICE`.
- **Everything below** — the gateway's runtime settings (`vogon/gateway/settings.py`).

Generated from the settings class (`vogon/gateway/settings.py`); regenerate when a key changes.


## Deployment and storage

| setting | env | type | default | notes |
|---|---|---|---|---|
| `storage_base_path` | `STORAGE_BASE_PATH` | `str` | `"./storage"` |  |
| `recordings_path` | `RECORDINGS_PATH` | `str` | `"./storage/recordings"` |  |
| `max_call_duration_sec` | `MAX_CALL_DURATION_SEC` | `int` | `600` | 10 minutes — also controls receive loop timeout |
| `public_base_url` | `PUBLIC_BASE_URL` | `str` | `"http://localhost:8001"` | Publicly reachable base URL for this service (used to build TwiML callback URLs). Set to your ngrok URL in dev or your production domain in prod. |
| `s3_enabled` | `S3_ENABLED` | `bool` | `False` |  |
| `s3_bucket` | `S3_BUCKET` | `str` | `""` | S3_BUCKET — required when s3_enabled |

## Provider credentials and models

| setting | env | type | default | notes |
|---|---|---|---|---|
| `deepgram_api_key` | `DEEPGRAM_API_KEY` | `str` | `""` |  |
| `deepgram_stt_model` | `DEEPGRAM_STT_MODEL` | `str` | `"flux-general-en"` |  |
| `deepgram_tts_model` | `DEEPGRAM_TTS_MODEL` | `str` | `"aura-2-thalia-en"` |  |
| `cerebras_api_key` | `CEREBRAS_API_KEY` | `str` | `""` |  |
| `cerebras_model` | `CEREBRAS_MODEL` | `str` | `"gpt-oss-120b"` |  |
| `openai_api_key` | `OPENAI_API_KEY` | `str` | `""` |  |
| `twilio_account_sid` | `TWILIO_ACCOUNT_SID` | `str` | `""` |  |
| `twilio_auth_token` | `TWILIO_AUTH_TOKEN` | `str` | `""` |  |
| `twilio_phone_number` | `TWILIO_PHONE_NUMBER` | `str` | `""` |  |
| `open_router_api_key` | `OPEN_ROUTER_API_KEY` | `str` | `""` | OPEN_ROUTER_API_KEY — The gateway's own decisions calls (the front model's move, the line detector's recording question) go to Jev over OpenRouter (vogon/gateway/jev.py). |
| `jev_model` | `JEV_MODEL` | `str` | `"typesafe/jev-1.13"` |  |
| `jev_timeout_ms` | `JEV_TIMEOUT_MS` | `int` | `4000` | one decisions call sits on the turn's critical path |

## Metrics

| setting | env | type | default | notes |
|---|---|---|---|---|
| `metrics_namespace` | `METRICS_NAMESPACE` | `str` | `"vogon"` | The OpenTelemetry metric and attribute namespace (metrics/otel.py): every metric is ``<namespace>.<name>``, the resource's service.namespace is this. A deployme |

## Redis (the session store's backend and the audio event stream)

| setting | env | type | default | notes |
|---|---|---|---|---|
| `redis_enabled` | `REDIS_ENABLED` | `bool` | `True` | Redis is ON by default (decision 2026-08-26): the dev environment keeps a local Redis always running (`tools/redis-local.sh start`, port 6390) and the stateless |
| `redis_url` | `REDIS_URL` | `str` | `"redis://127.0.0.1:6390"` |  |
| `redis_cluster_mode` | `REDIS_CLUSTER_MODE` | `bool` | `False` | ElastiCache Serverless is always cluster-mode — prod sets True in env. False = standalone Redis (the local tools/redis-local.sh instance). |

## The turn service (the application behind the Turn API)

| setting | env | type | default | notes |
|---|---|---|---|---|
| `turn_service_api_key` | `TURN_SERVICE_API_KEY` | `str` | `""` | Turn service HTTP seam (Phase 1 M1): bearer token the gateway presents on /turn/v1/* calls. Empty = auth disabled (dev / same-host text mode). |
| `turn_service_url` | `TURN_SERVICE_URL` | `str` | `""` | Full Turn API root of the turn service, e.g. http://localhost:8001/turn/v1 — the client appends only the operation name (§2.2). This is the FALLBACK: a call nor |
| `turn_service_timeout_sec` | `TURN_SERVICE_TIMEOUT_SEC` | `float` | `40.0` | Request timeout for turn-service calls. end_call awaits the call-end tools server-side (up to 20 s), so this must comfortably exceed that. |
| `turn_retry_delays` | `TURN_RETRY_DELAYS` | `str` | `"0.25,1,2,4,8"` | Retry ladder for turn-service calls (seconds between attempts; attempts = len+1). Spans ~15 s so a systemd restart (RestartSec=10) is survivable — the filler ho |

## STT — Deepgram endpointing

| setting | env | type | default | notes |
|---|---|---|---|---|
| `deepgram_eot_threshold` | `DEEPGRAM_EOT_THRESHOLD` | `float` | `0.9` |  |
| `deepgram_eot_timeout_ms` | `DEEPGRAM_EOT_TIMEOUT_MS` | `int` | `1000` | browser/widget: silence before Deepgram forces EOT |
| `deepgram_eot_hold_ms` | `DEEPGRAM_EOT_HOLD_MS` | `int` | `250` | legacy hold when eager EOT disabled |
| `deepgram_eager_eot_threshold` | `DEEPGRAM_EAGER_EOT_THRESHOLD` | `float` | `0.72` | Eager EOT: speculative pipeline on EagerEndOfTurn; TTS only on EndOfTurn. |
| `deepgram_phone_eot_timeout_ms` | `DEEPGRAM_PHONE_EOT_TIMEOUT_MS` | `int` | `1400` | Phone (Twilio 8kHz): slightly looser EOT than browser — narrowband + longer pauses. |
| `deepgram_phone_eot_threshold` | `DEEPGRAM_PHONE_EOT_THRESHOLD` | `float` | `0.85` |  |
| `deepgram_phone_eager_eot_threshold` | `DEEPGRAM_PHONE_EAGER_EOT_THRESHOLD` | `float` | `0.70` |  |
| `stt_word_conf_threshold` | `STT_WORD_CONF_THRESHOLD` | `float` | `0.75` | per-word confidence below which a word is "low confidence" — STT low-confidence gate — flag/act on garbled utterances (transcription confidence too low to trust). See docs. The shadow-first rollout is |
| `stt_feed_mode` | `STT_FEED_MODE` | `str` | `"open"` | Open STT feed (docs/stt-open-feed.md). While the agent's audio is active, caller audio historically reached Deepgram only after the barge gate confirmed a loud- |

## TTS and phone audio

| setting | env | type | default | notes |
|---|---|---|---|---|
| `tts_pacing_mode` | `TTS_PACING_MODE` | `str` | `"sentence"` | sentence / burst — Bounded playout buffer (full-duplex-semantic-barge-in §6, Phase 3). sentence = per-sentence TTS segments + paced release: Twilio holds only tts_buffer_lead_ms o |
| `tts_buffer_lead_ms` | `TTS_BUFFER_LEAD_MS` | `int` | `500` | audio Twilio may buffer ahead of real time |
| `tts_phone_encoding` | `TTS_PHONE_ENCODING` | `str` | `"mulaw"` | TTS audio profile for Twilio phone calls. "mulaw" at 8000 Hz requests phone-optimised audio directly from Deepgram, bypassing Python-side resampling. Twilio Med |
| `tts_phone_sample_rate` | `TTS_PHONE_SAMPLE_RATE` | `int` | `8000` |  |

## Barge-in — energy, VAD, confirmation, the floor

| setting | env | type | default | notes |
|---|---|---|---|---|
| `barge_controller_enabled` | `BARGE_CONTROLLER_ENABLED` | `bool` | `True` |  |
| `barge_episode_gap_ms` | `BARGE_EPISODE_GAP_MS` | `int` | `350` |  |
| `barge_repeat_speech_ms` | `BARGE_REPEAT_SPEECH_MS` | `int` | `240` |  |
| `barge_persistent_speech_ms` | `BARGE_PERSISTENT_SPEECH_MS` | `int` | `1200` |  |
| `barge_attempt_history_ms` | `BARGE_ATTEMPT_HISTORY_MS` | `int` | `10000` |  |
| `barge_floor_silence_ms` | `BARGE_FLOOR_SILENCE_MS` | `int` | `500` |  |
| `barge_in_rms_threshold` | `BARGE_IN_RMS_THRESHOLD` | `int` | `1200` | int16 RMS a cleaned frame must exceed (~−26 dBFS) — Barge-in (phone / Twilio RMS gate). See docs/barge-in-tuning.md. Tier 1: cancel TTS only after N consecutive cleaned frames exceed the threshold, an |
| `barge_in_consecutive_frames` | `BARGE_IN_CONSECUTIVE_FRAMES` | `int` | `3` | sustained over-threshold frames before cancelling (×20 ms) |
| `barge_in_grace_ms` | `BARGE_IN_GRACE_MS` | `int` | `100` | ignore barge this long after TTS starts |
| `barge_in_adaptive_enabled` | `BARGE_IN_ADAPTIVE_ENABLED` | `bool` | `False` | Phase A — adaptive threshold (docs/barge-in-tuning.md). Tracks the line's own noise floor (EMA over noise frames only) so quiet lines get sensitive (~600) and l |
| `barge_in_adaptive_alpha` | `BARGE_IN_ADAPTIVE_ALPHA` | `float` | `0.05` | noise-floor EMA weight (noise frames only) |
| `barge_in_adaptive_margin` | `BARGE_IN_ADAPTIVE_MARGIN` | `float` | `7.0` | threshold = clamp(margin × floor, min, max) |
| `barge_in_adaptive_min` | `BARGE_IN_ADAPTIVE_MIN` | `int` | `600` | never less sensitive than the soft-speech band |
| `barge_in_adaptive_max` | `BARGE_IN_ADAPTIVE_MAX` | `int` | `2600` | never above direct caller speech |
| `barge_in_adaptive_warmup_frames` | `BARGE_IN_ADAPTIVE_WARMUP_FRAMES` | `int` | `50` | noise frames before the estimate is trusted (1 s) |
| `barge_in_confirm` | `BARGE_IN_CONFIRM` | `str` | `"off"` | "off" / "local_vad" — Phase B1 — speech-gated confirm (BargeGate FSM). "off" = Tier-1 behaviour (energy debounce confirms directly); "local_vad" = a webrtcvad mode-3 M-of-N confirm o |
| `barge_in_vad_aggressiveness` | `BARGE_IN_VAD_AGGRESSIVENESS` | `int` | `3` | webrtcvad mode (0 permissive … 3 strict) |
| `barge_in_confirm_frames_m` | `BARGE_IN_CONFIRM_FRAMES_M` | `int` | `3` | confirm = M speech-positive … |
| `barge_in_confirm_frames_n` | `BARGE_IN_CONFIRM_FRAMES_N` | `int` | `5` | … of the last N provisional frames |
| `barge_in_confirm_timeout_ms` | `BARGE_IN_CONFIRM_TIMEOUT_MS` | `int` | `400` | provisional window before ABORT |
| `barge_in_confirm_energy_floor_frames` | `BARGE_IN_CONFIRM_ENERGY_FLOOR_FRAMES` | `int` | `3` | K quiet frames inside the window → early ABORT |
| `barge_in_frame_log` | `BARGE_IN_FRAME_LOG` | `bool` | `True` | Per-frame RMS log (docs/barge-in-tuning.md): raw/clean RMS, the gate's live threshold and noise floor, plus a mono post-AEC WAV of what the gate heard. Writes < |

## Semantic barge judge

| setting | env | type | default | notes |
|---|---|---|---|---|
| `semantic_barge_enabled` | `SEMANTIC_BARGE_ENABLED` | `bool` | `True` | Full-duplex semantic barge-in (docs/full-duplex-semantic-barge-in.md). A confirmed energy barge opens a yield-pending window (audio keeps playing) while a heuri |
| `semantic_barge_window_ms` | `SEMANTIC_BARGE_WINDOW_MS` | `int` | `900` | yield-pending budget |
| `semantic_barge_llm_timeout_ms` | `SEMANTIC_BARGE_LLM_TIMEOUT_MS` | `int` | `700` | Cerebras verdict cap (first time-capped LLM call) |
| `semantic_barge_backchannel_max_tokens` | `SEMANTIC_BARGE_BACKCHANNEL_MAX_TOKENS` | `int` | `3` | heuristic floor: <=N lexicon tokens = backchannel |
| `semantic_barge_substantive_min_tokens` | `SEMANTIC_BARGE_SUBSTANTIVE_MIN_TOKENS` | `int` | `6` | heuristic floor: >=N tokens = real interrupt |

## The front model (the filler's source of words)

| setting | env | type | default | notes |
|---|---|---|---|---|
| `front_model_enabled` | `FRONT_MODEL_ENABLED` | `bool` | `True` | The filler hold's source of words on the voice gateway: one sentence composed for the moment by a small model, or a beat from an approved pool. DEFAULT ON (deci |
| `front_model` | `FRONT_MODEL` | `str` | `"qwen-3.8-27b"` | Cerebras model id |
| `front_model_effort` | `FRONT_MODEL_EFFORT` | `str` | `"none"` | reasoning_effort; "none" = no reasoning tokens |
| `front_config_path` | `FRONT_CONFIG_PATH` | `str` | `""` | The domain's front artifact (prompt + beat pools + must_not_say). Empty = the model tier is OFF; the hold uses the call's FillerPolicy pools. examples/front.yaml is a neutral template. |
| `front_beat_after_ms` | `FRONT_BEAT_AFTER_MS` | `int` | `900` | hold still open, the model LATE or failed → a beat; also the request's timeout |
| `front_beat_after_empty_ms` | `FRONT_BEAT_AFTER_EMPTY_MS` | `int` | `1500` | hold still open, the model deliberately said NOTHING → a beat, later |
| `front_wait_again_after_ms` | `FRONT_WAIT_AGAIN_AFTER_MS` | `int` | `2500` | answer still not back after a front line → one more beat |
| `front_max_words` | `FRONT_MAX_WORDS` | `int` | `12` | one sentence, no exceptions |
| `front_min_gap_sec` | `FRONT_MIN_GAP_SEC` | `float` | `6.0` | minimum spacing between any two front lines |
| `front_engine` | `FRONT_ENGINE` | `str` | `"jev"` | Which engine reads the turn for the front voice. "jev"  — one decisions call: what move will the DESK open with, plus riders. The words come from approved pools |
| `front_move_floor` | `FRONT_MOVE_FLOOR` | `float` | `0.45` | below this confidence on the desk move, say nothing |
| `front_repeat_enabled` | `FRONT_REPEAT_ENABLED` | `bool` | `True` | the "say that again" short-circuit |

## Ambience

| setting | env | type | default | notes |
|---|---|---|---|---|
| `ambience_enabled` | `AMBIENCE_ENABLED` | `bool` | `False` | master switch (widget + phone) — Call ambience (liveliness background sound) — transport-layer only. See docs/ambience-liveliness.md. OFF by default until tuned + listened to; Phase 1 ships the |
| `ambience_room_tone` | `AMBIENCE_ROOM_TONE` | `bool` | `True` | continuous room-tone bed |
| `ambience_typing` | `AMBIENCE_TYPING` | `bool` | `True` | typing cue in the STT→TTS gap |
| `ambience_oneshots` | `AMBIENCE_ONESHOTS` | `bool` | `False` | sparse one-shots (ring/creak) — phone only, lowest priority |
| `ambience_typing_delay_ms` | `AMBIENCE_TYPING_DELAY_MS` | `int` | `0` | hold after STT-final before typing (0 = immediate) |
| `ambience_typing_gain` | `AMBIENCE_TYPING_GAIN` | `float` | `0.15` | typing loudness — well below the room bed / speech |
| `ambience_room_gain` | `AMBIENCE_ROOM_GAIN` | `float` | `1.0` | trim on the (already-quiet) room bed |
| `ambience_duck_factor` | `AMBIENCE_DUCK_FACTOR` | `float` | `0.5` | room gain while TTS speaks (−6 dB) |
| `ambience_oneshot_min_gap_sec` | `AMBIENCE_ONESHOT_MIN_GAP_SEC` | `float` | `25.0` | Phone (Twilio) between-turn one-shot cadence: minimum quiet seconds before a one-shot may fire, so they stay sparse and non-intrusive. |

## Live (realtime) session

| setting | env | type | default | notes |
|---|---|---|---|---|
| `live_model` | `LIVE_MODEL` | `str` | `"gpt-live-1"` | GPT-Live 1 — the gateway's LiveTransport (docs/GPT_LIVE_TURN_API_DESIGN.md). Uses openai_api_key. The model's own voice is never relayed while the pa |
| `live_voice` | `LIVE_VOICE` | `str` | `"marin"` |  |
| `live_final_quiet_ms` | `LIVE_FINAL_QUIET_MS` | `int` | `900` | fallback end-of-turn if the model never delegates |
| `live_start_timeout_s` | `LIVE_START_TIMEOUT_S` | `float` | `8.0` | hard deadline for the background open |
| `live_start_wait_ms` | `LIVE_START_WAIT_MS` | `int` | `300` | how long start() waits before letting the greeting go |
