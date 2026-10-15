"""The gateway's configuration — three layers, explicitly modeled.

Design: docs/voice-gateway-phase1-standalone.md §2.5. Every value the
gateway operates on is attributable to exactly one layer:

  Layer 1  GatewayConfig   this DEPLOYMENT: auth keys, public URL, store
                           choice, tenant-profile source. Env (GATEWAY_*).
  Layer 2  TenantProfile   this TENANT: the audio-domain defaults, in a
                           versioned schema a control plane can serve later.
                           Source today: built-in defaults (taken from the
                           shared settings, so behavior matches the
                           monolith) + optional per-tenant overrides from a
                           JSON file (GATEWAY_TENANT_PROFILES).
  Layer 3  CallConfig      this CALL: what the application returns from
                           start_call (already the contract).

Precedence CallConfig > TenantProfile > defaults, resolved ONCE at call
start into a frozen snapshot stored on the CallSessionRecord — so any
replica, a resumed call, and the audit trail all see the same values.

M1 honesty note: the deep DSP tunables (AEC tail, barge-gate debounce, STT
EOT thresholds …) are IN the TenantProfile schema but the transports still
read them from the shared process settings, exactly as the monolith does —
rewiring every transport read to the snapshot is follow-through recorded in
the plan. The snapshot is authoritative for the blocks the DRIVER consumes
(thresholds, filler, ambience, recording, barge policy).
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import BaseModel
from pydantic_settings import BaseSettings

from vogon.gateway.settings import settings as shared_settings
from vogon_contract import CallConfig

logger = logging.getLogger(__name__)

PROFILE_SCHEMA_VERSION = 1


class GatewayConfig(BaseSettings):
    """Layer 1 — deployment config. Env-prefixed GATEWAY_*."""

    # Static service secret for the app↔gateway pair (decision 2026-08-25:
    # service-to-service edges use one static key per PAIR; customer
    # identity stays at the APP's front door, validated once by the
    # control plane — never re-validated here). Presented by the app on
    # /v1/tenants/... calls AND by the gateway on its status relay back.
    # Empty = auth disabled (dev). Tenant-scoped API keys return only if
    # the gateway ever grows a direct-customer surface ("gateway as a
    # product"), minted by the control plane at that point.
    service_key: str = ""
    public_base_url: str = ""              # webhooks + ws_url host
    log_level: str = "INFO"                # DEBUG|INFO|WARNING — GATEWAY_LOG_LEVEL
    session_store: str = "memory"          # memory | redis
    tenant_profiles: str = ""              # path to {tenant: {...}} JSON
    # D5: what to do when a native audio capability (AEC/VAD/audioop) is
    # missing at boot. "refuse" (default — a phone deployment must not run
    # degraded) | "warn" (dev / text-only).
    capability_policy: str = "refuse"
    # Twilio <Say> voice for the escalation lines (hold, whisper, voicemail,
    # callback) — GATEWAY_ESCALATION_SAY_VOICE. Twilio's TTS, so a Polly or
    # Google voice name; the default is the one the flow always used.
    escalation_say_voice: str = "Polly.Joanna-Neural"
    # Twilio credentials intentionally stay in the SHARED env for M1 (one
    # repo, one .env); the schema slot exists so a split env is a value
    # change, not a redesign.

    class Config:
        env_prefix = "GATEWAY_"
        env_file = ".env"
        extra = "ignore"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.service_key.strip())


class TenantProfile(BaseModel):
    """Layer 2 — per-tenant defaults, versioned schema v1.

    Grouped exactly as the plan's §2.5 table; a control plane serves this
    document later (M2) — the schema is the contract it fills.
    """

    schema_version: int = PROFILE_SCHEMA_VERSION

    # stt / tts
    stt_model: str = ""
    tts_model: str = ""
    tts_voice_default: str = ""
    language: str = "en-US"
    # barge
    semantic_barge_enabled: bool = True
    safety_phrases: list[str] = []
    floor_request_phrases: list[str] = []
    # ambience / filler / recording defaults (driver-consumed blocks)
    ambience_enabled: bool = False
    filler_enabled: bool = True
    recording_enabled: bool = True
    recording_channels: str = "stereo"
    # metrics
    metrics_root: str = ""                 # empty = the shared default

    @classmethod
    def built_in(cls) -> TenantProfile:
        """The defaults every tenant starts from — taken from the shared
        settings so a standalone gateway behaves exactly like the monolith
        until a profile says otherwise."""
        return cls(
            stt_model=getattr(shared_settings, "deepgram_stt_model", ""),
            tts_model=getattr(shared_settings, "deepgram_tts_model", ""),
            tts_voice_default=getattr(shared_settings, "tts_voice", "") or "",
            semantic_barge_enabled=getattr(
                shared_settings, "semantic_barge_enabled", True),
            ambience_enabled=getattr(shared_settings, "ambience_enabled", False),
        )


@lru_cache(maxsize=1)
def gateway_config() -> GatewayConfig:
    return GatewayConfig()


def _profile_overrides(path: str) -> dict:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        logger.warning("GATEWAY_TENANT_PROFILES=%s does not exist — "
                       "built-in defaults only", path)
        return {}
    try:
        return json.loads(p.read_text())
    except Exception as exc:
        logger.error("tenant profile file unreadable (%s) — refusing to "
                     "guess: %s", path, exc)
        raise


def tenant_profile(tenant: str, cfg: Optional[GatewayConfig] = None) -> TenantProfile:
    """Layer-2 resolution: built-in defaults overlaid by the tenant's entry
    in the profile file (unknown fields rejected by the schema — a typo'd
    profile fails loudly, never silently half-applies)."""
    cfg = cfg or gateway_config()
    base = TenantProfile.built_in()
    overrides = _profile_overrides(cfg.tenant_profiles).get(tenant)
    if not overrides:
        return base
    merged = {**base.model_dump(), **overrides}
    return TenantProfile(**merged)


def resolve_call_snapshot(profile: TenantProfile,
                          call_config: CallConfig) -> dict:
    """The precedence merge (CallConfig > TenantProfile > defaults), frozen
    as a plain dict for the session record. Answers "which config was this
    call under" for any replica and for the audit trail."""
    snap = {
        "profile": profile.model_dump(),
        "call_config": {k: v for k, v in asdict(call_config).items()
                        if v not in (None, [], {}, "")},
        "resolved": {
            "language": call_config.language or profile.language,
            "recording_enabled": (
                call_config.recording.enabled
                if call_config.recording is not None
                else profile.recording_enabled),
            "recording_channels": (
                call_config.recording.channels
                if call_config.recording is not None
                else profile.recording_channels),
            "semantic_barge_enabled": (
                call_config.barge_policy.semantic_judge
                if call_config.barge_policy is not None
                else profile.semantic_barge_enabled),
            "safety_phrases": (
                call_config.barge_policy.safety_phrases
                if call_config.barge_policy is not None
                else profile.safety_phrases),
            "floor_request_phrases": (
                call_config.barge_policy.floor_request_phrases
                if call_config.barge_policy is not None
                else profile.floor_request_phrases),
            "filler": call_config.filler is not None or profile.filler_enabled,
            "ambience": (call_config.ambience or {}).get(
                "enabled", profile.ambience_enabled),
        },
    }
    return snap
