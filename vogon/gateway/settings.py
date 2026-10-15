"""Gateway settings — the voice gateway's OWN settings object.

Every key the gateway reads, with the same environment variable names and
defaults as the application's shared `Settings` had for them, so an existing
.env keeps working. The gateway process reads its .env from its own root and
ignores keys that belong to other services (one .env may serve several).

Kept in step with the application copy by tests/test_gateway_settings.py while
both exist; the application copy goes when the turn service stops reading
these keys (vogon step 2, 2026-10-10).
"""
from __future__ import annotations

import os

from pydantic_settings import BaseSettings


class GatewaySettings(BaseSettings):
    # ── Deployment and storage ──────────────────────────────────────────────────
    storage_base_path: str = "./storage"
    recordings_path: str = "./storage/recordings"
    max_call_duration_sec: int = 600   # 10 minutes — also controls receive loop timeout
    # Publicly reachable base URL for this service (used to build TwiML callback URLs).
    # Set to your ngrok URL in dev or your production domain in prod.
    public_base_url: str = "http://localhost:8001"
    s3_enabled: bool = False
    s3_bucket: str = ""                 # S3_BUCKET — required when s3_enabled

    # ── Provider credentials and models ─────────────────────────────────────────
    deepgram_api_key: str = ""
    deepgram_stt_model: str = "flux-general-en"
    deepgram_tts_model: str = "aura-2-thalia-en"
    cerebras_api_key: str = ""
    cerebras_model: str = "gpt-oss-120b"
    openai_api_key: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    # The gateway's own decisions calls (the front model's move, the line
    # detector's recording question) go to Jev over OpenRouter (vogon/gateway/jev.py).
    open_router_api_key: str = ""      # OPEN_ROUTER_API_KEY
    jev_model: str = "typesafe/jev-1.13"
    jev_timeout_ms: int = 4000         # one decisions call sits on the turn's critical path

    # ── Metrics ───────────────────────────────────────────────────────────────
    # The OpenTelemetry metric and attribute namespace (metrics/otel.py): every
    # metric is ``<namespace>.<name>``, the resource's service.namespace is this.
    # A deployment that already has dashboards on another prefix sets it.
    metrics_namespace: str = "vogon"

    # ── Redis (the session store's backend and the audio event stream) ─────────
    # Redis is ON by default (decision 2026-08-26): the dev environment keeps
    # a local Redis always running (`tools/redis-local.sh start`, port 6390)
    # and the stateless turn service depends on the session store being real.
    # Defaults below are the LOCAL DEV values; production env files MUST set
    # REDIS_URL and REDIS_CLUSTER_MODE=1 explicitly (ElastiCache, TLS,
    # cluster mode). Unit tests run hermetic: the pytest conftest forces
    # redis_enabled=False so suites use the in-process store.
    redis_enabled: bool = True
    redis_url: str = "redis://127.0.0.1:6390"
    # ElastiCache Serverless is always cluster-mode — prod sets True in env.
    # False = standalone Redis (the local tools/redis-local.sh instance).
    redis_cluster_mode: bool = False

    # ── The turn service (the application behind the Turn API) ──────────────────
    # Turn service HTTP seam (Phase 1 M1): bearer token the gateway presents
    # on /turn/v1/* calls. Empty = auth disabled (dev / same-host text mode).
    turn_service_api_key: str = ""
    # Full Turn API root of the turn service, e.g.
    # http://localhost:8001/turn/v1 — the client appends only the operation
    # name (§2.2). This is the FALLBACK: a call normally carries its own
    # address in the session record's turn_service binding. Empty with no
    # binding is a configuration error (TurnServiceNotConfigured), not a
    # mode — the in-process Turn API client was deleted 2026-08-29.
    turn_service_url: str = ""
    # Request timeout for turn-service calls. end_call awaits the call-end
    # tools server-side (up to 20 s), so this must comfortably exceed that.
    turn_service_timeout_sec: float = 40.0
    # Retry ladder for turn-service calls (seconds between attempts;
    # attempts = len+1). Spans ~15 s so a systemd restart (RestartSec=10)
    # is survivable — the filler hold masks the gap for utterance turns.
    # Retries are safe by construction: the server serializes per-call and
    # the committed response rides the checkpoint (last_response).
    turn_retry_delays: str = "0.25,1,2,4,8"

    # ── STT — Deepgram endpointing ──────────────────────────────────────────────
    deepgram_eot_threshold: float = 0.9
    deepgram_eot_timeout_ms: int = 1000   # browser/widget: silence before Deepgram forces EOT
    deepgram_eot_hold_ms: int = 250       # legacy hold when eager EOT disabled
    # Eager EOT: speculative pipeline on EagerEndOfTurn; TTS only on EndOfTurn.
    deepgram_eager_eot_threshold: float = 0.72
    # Phone (Twilio 8kHz): slightly looser EOT than browser — narrowband + longer pauses.
    deepgram_phone_eot_timeout_ms: int = 1400
    deepgram_phone_eot_threshold: float = 0.85
    deepgram_phone_eager_eot_threshold: float = 0.70
    # STT low-confidence gate — flag/act on garbled utterances (transcription
    # confidence too low to trust). See docs. The shadow-first rollout is
    # OVER: production has run this in enforce for long enough that shadow was
    # only ever a default nobody deployed, and a code default that disagrees
    # with production means every local run, every sim and the whole eval
    # suite replayed a call the callers never get (the domain's clarification
    # ladder and its graceful close were never exercised). Enforce is the
    # deployed behaviour and now the default; set STT_GATE_MODE=shadow to
    # calibrate.
    stt_word_conf_threshold: float = 0.75          # per-word confidence below which a word is "low confidence"
    # Open STT feed (docs/stt-open-feed.md). While the agent's audio is active,
    # caller audio historically reached Deepgram only after the barge gate
    # confirmed a loud-enough barge — quiet or overlapping speech was silently
    # dropped, so Deepgram heard clipped audio and fragmented the caller's turn.
    #   open  — the AEC-cleaned caller audio streams to Deepgram at ALL times;
    #           the barge gate keeps only its playback-cut decision (default).
    #   gated — the old valve: caller audio during agent playback reaches
    #           Deepgram only via a confirmed barge (kill switch).
    stt_feed_mode: str = "open"

    # ── TTS and phone audio ─────────────────────────────────────────────────────
    # Bounded playout buffer (full-duplex-semantic-barge-in §6, Phase 3).
    # sentence = per-sentence TTS segments + paced release: Twilio holds only
    # tts_buffer_lead_ms of audio, so a hard barge cut loses <= the lead and an
    # answer_to_heard yields gracefully at the sentence boundary (no clear).
    # burst = the pre-Phase-3 whole-utterance run-ahead (kill switch).
    #
    # The lead is also the bound on SENT vs PLAYED: a barge clears Twilio's
    # buffer, so up to one lead of already-sent audio is never heard. Every
    # "sent means heard" claim downstream — the delivery report's sentence
    # counts, the arming gate — is accurate to within it.
    # See docs/voice-timing-constants.md §3.
    tts_pacing_mode: str = "sentence"              # sentence | burst
    tts_buffer_lead_ms: int = 500                  # audio Twilio may buffer ahead of real time
    # TTS audio profile for Twilio phone calls.
    # "mulaw" at 8000 Hz requests phone-optimised audio directly from Deepgram,
    # bypassing Python-side resampling. Twilio Media Streams only accept mulaw
    # 8kHz on the wire — set tts_phone_encoding="linear16" (with tts_phone_sample_rate=24000)
    # only to revert to the legacy audioop downsampling path for debugging.
    tts_phone_encoding: str = "mulaw"
    tts_phone_sample_rate: int = 8000

    # ── Barge-in — energy, VAD, confirmation, the floor ─────────────────────────
    barge_controller_enabled: bool = True
    barge_episode_gap_ms: int = 350
    barge_repeat_speech_ms: int = 240
    barge_persistent_speech_ms: int = 1200
    barge_attempt_history_ms: int = 10000
    barge_floor_silence_ms: int = 500
    # Barge-in (phone / Twilio RMS gate). See docs/barge-in-tuning.md. Tier 1:
    # cancel TTS only after N consecutive cleaned frames exceed the threshold,
    # and never within the start-grace. Defaults are provisional (from a dBFS
    # analysis, not yet measured call data — the barge_in_event log grounds them).
    barge_in_rms_threshold: int = 1200        # int16 RMS a cleaned frame must exceed (~−26 dBFS)
    barge_in_consecutive_frames: int = 3      # sustained over-threshold frames before cancelling (×20 ms)
    barge_in_grace_ms: int = 100              # ignore barge this long after TTS starts
    # Phase A — adaptive threshold (docs/barge-in-tuning.md). Tracks the line's
    # own noise floor (EMA over noise frames only) so quiet lines get sensitive
    # (~600) and loud backgrounds get strict (~2600). Params from the Phase 0
    # measurement (2026-07: quiet-line floor p50≈6–35, noisy≈85–300; caller
    # speech ≈1500–4300). Fixed threshold is the cold-start + disabled fallback.
    barge_in_adaptive_enabled: bool = False
    barge_in_adaptive_alpha: float = 0.05     # noise-floor EMA weight (noise frames only)
    barge_in_adaptive_margin: float = 7.0     # threshold = clamp(margin × floor, min, max)
    barge_in_adaptive_min: int = 600          # never less sensitive than the soft-speech band
    barge_in_adaptive_max: int = 2600         # never above direct caller speech
    barge_in_adaptive_warmup_frames: int = 50  # noise frames before the estimate is trusted (1 s)
    # Phase B1 — speech-gated confirm (BargeGate FSM). "off" = Tier-1 behaviour
    # (energy debounce confirms directly); "local_vad" = a webrtcvad mode-3
    # M-of-N confirm over a provisional window (measured: mode 3 is the only
    # usable mode — 2.6–3% false during TTS on quiet lines; TV/music background
    # still defeats any VAD, which is what the adaptive threshold is for).
    barge_in_confirm: str = "off"             # "off" | "local_vad"
    barge_in_vad_aggressiveness: int = 3      # webrtcvad mode (0 permissive … 3 strict)
    barge_in_confirm_frames_m: int = 3        # confirm = M speech-positive …
    barge_in_confirm_frames_n: int = 5        # … of the last N provisional frames
    barge_in_confirm_timeout_ms: int = 400    # provisional window before ABORT
    barge_in_confirm_energy_floor_frames: int = 3  # K quiet frames inside the window → early ABORT
    # Per-frame RMS log (docs/barge-in-tuning.md): raw/clean RMS, the gate's
    # live threshold and noise floor, plus a mono post-AEC WAV of what the
    # gate heard. Writes <recordings_path>/<call_id>.frames.csv + .clean.wav
    # at call end. ON by default since 2026-09-30: call b59be90d's floor
    # defect (a 220 ms wordless burst discarding the reply) could only be
    # read off `speech_ms` because this was off in production — a floor or
    # barge decision is unexplainable without the frames behind it. Cost: one
    # tuple + 160 bytes per 20 ms frame, written once at teardown.
    barge_in_frame_log: bool = True

    # ── Semantic barge judge ────────────────────────────────────────────────────
    # Full-duplex semantic barge-in (docs/full-duplex-semantic-barge-in.md).
    # A confirmed energy barge opens a yield-pending window (audio keeps playing)
    # while a heuristic floor + a time-capped Cerebras verdict decide: absorb
    # (backchannel — never cut) vs cut. Twilio transport only (Phase 1).
    semantic_barge_enabled: bool = True
    semantic_barge_window_ms: int = 900            # yield-pending budget
    semantic_barge_llm_timeout_ms: int = 700       # Cerebras verdict cap (first time-capped LLM call)
    semantic_barge_backchannel_max_tokens: int = 3 # heuristic floor: <=N lexicon tokens = backchannel
    semantic_barge_substantive_min_tokens: int = 6 # heuristic floor: >=N tokens = real interrupt

    # ── The front model (the filler's source of words) ──────────────────────────
    # The filler hold's source of words on the voice gateway: one sentence
    # composed for the moment by a small model, or a beat from an approved
    # pool. DEFAULT ON (decision 2026-09-19: run it for real); FRONT_MODEL_ENABLED=0
    # restores the canned FillerPolicy phrases exactly as before. Read by the
    # GATEWAY process; the turn service does not gate on it.
    front_model_enabled: bool = True
    front_model: str = "qwen-3.8-27b"              # Cerebras model id
    front_model_effort: str = "none"               # reasoning_effort; "none" = no reasoning tokens
    # The domain's front artifact (prompt + beat pools + must_not_say).
    # Empty = the model tier is OFF; the hold uses the call's FillerPolicy pools.
    # examples/front.yaml is a neutral template.
    front_config_path: str = ""
    front_beat_after_ms: int = 900                 # hold still open, the model LATE or failed → a beat; also the request's timeout
    front_beat_after_empty_ms: int = 1500          # hold still open, the model deliberately said NOTHING → a beat, later
    front_wait_again_after_ms: int = 2500          # answer still not back after a front line → one more beat
    front_max_words: int = 12                      # one sentence, no exceptions
    front_min_gap_sec: float = 6.0                 # minimum spacing between any two front lines
    # Which engine reads the turn for the front voice.
    #   "jev"  — one decisions call: what move will the DESK open with, plus
    #            riders. The words come from approved pools, so a wrong read
    #            costs a wrong line, never an unsafe one. Default.
    #   "llm"  — the original free-text composer (docs/voice-gateway-front-model.md).
    #   "off"  — beats only, the pre-model behaviour.
    front_engine: str = "jev"
    front_move_floor: float = 0.45                 # below this confidence on the desk move, say nothing
    front_repeat_enabled: bool = True              # the "say that again" short-circuit

    # ── Ambience ────────────────────────────────────────────────────────────────
    # Call ambience (liveliness background sound) — transport-layer only.
    # See docs/ambience-liveliness.md. OFF by default until tuned + listened to;
    # Phase 1 ships the mixer core with nothing wired, so this is inert for now.
    ambience_enabled: bool = False        # master switch (widget + phone)
    ambience_room_tone: bool = True       # continuous room-tone bed
    ambience_typing: bool = True          # typing cue in the STT→TTS gap
    ambience_oneshots: bool = False       # sparse one-shots (ring/creak) — phone only, lowest priority
    ambience_typing_delay_ms: int = 0     # hold after STT-final before typing (0 = immediate)
    ambience_typing_gain: float = 0.15    # typing loudness — well below the room bed / speech
    ambience_room_gain: float = 1.0       # trim on the (already-quiet) room bed
    ambience_duck_factor: float = 0.5     # room gain while TTS speaks (−6 dB)
    # Phone (Twilio) between-turn one-shot cadence: minimum quiet seconds before
    # a one-shot may fire, so they stay sparse and non-intrusive.
    ambience_oneshot_min_gap_sec: float = 25.0

    # ── Live (realtime) session ─────────────────────────────────────────────────
    # GPT-Live 1 — the gateway's LiveTransport (docs/GPT_LIVE_TURN_API_DESIGN.md).
    # Uses openai_api_key. The model's own voice is never relayed while the
    # path is recite-only, so live_voice is inert until generate lines exist.
    live_model: str = "gpt-live-1"
    live_voice: str = "marin"
    live_final_quiet_ms: int = 900      # fallback end-of-turn if the model never delegates
    live_start_timeout_s: float = 8.0     # hard deadline for the background open
    live_start_wait_ms: int = 300         # how long start() waits before letting the greeting go

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        # One .env may serve several services (the application, this gateway,
        # workers): keys of another service's settings class are ignored, not
        # fatal (prod outage 2026-08-25).
        extra = "ignore"

    def resolved_front_config_path(self) -> str:
        """FRONT_CONFIG_PATH as an absolute path, or "" when unset. The front
        model's config is DOMAIN content (which lines the filler may say), so
        the gateway ships no default of its own: unset means the model tier is
        off and the hold draws on the call's FillerPolicy pools."""
        if self.front_config_path:
            return os.path.abspath(self.front_config_path)
        return ""


settings = GatewaySettings()
