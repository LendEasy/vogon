"""ArtifactStore — where the gateway's call artifacts go (M1 item 8).

Design: voice-gateway-phase1-standalone.md §3.2. Recording is a GATEWAY
artifact; the application decides IF a call is recorded
(CallConfig.recording), the gateway decides HOW and WHERE. This module is
the "where": an injected store interface with the two implementations the
monolith already used implicitly — local disk and S3 — now explicit, so
the recorder no longer reads `settings` / `app.services` directly (fence
item 10) and a future deployment can swap the backend without touching
audio code.

Layout note (amendment to §3.2's `{tenant}/{call_id}/…` sketch): keys stay
FLAT, keyed by call_id — call_ids are gateway-minted UUIDs (globally
unique, no cross-tenant collision), and both the monolith's artifact
routes and the S3 archive worker read the existing layout. Tenancy is
enforced at the gateway's artifact ROUTES (a file is only served through
a tenant-checked session record), not by directory shape.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Protocol

import aiofiles
import boto3

from vogon.gateway import settings as _gw_settings

logger = logging.getLogger(__name__)


class ArtifactStore(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...


class LocalDirStore:
    """Artifacts under a base directory. Key = relative path."""

    def __init__(self, base: str | Path) -> None:
        self._base = Path(base)

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._base / key
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(path, "wb") as f:
            await f.write(data)
        logger.info("Artifact saved: %s (%.1f KB)", path, len(data) / 1024)


class S3Store:
    """Artifacts to S3. Own boto3 client — no app.services import."""

    def __init__(self, bucket: str, region: str = "") -> None:
        self._bucket = bucket
        self._region = region
        self._client = None

    def _s3(self):
        if self._client is None:
            self._client = boto3.client(
                "s3", **({"region_name": self._region} if self._region else {}))
        return self._client

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self._s3().put_object(
                Bucket=self._bucket, Key=key, Body=data,
                ContentType=content_type),
        )
        logger.info("Artifact uploaded: s3://%s/%s", self._bucket, key)


def default_recording_stores() -> list[tuple[ArtifactStore, str]]:
    """(store, key_template) pairs from the gateway settings — exactly the
    monolith's behavior: local always, S3 when enabled. The key templates
    preserve today's layouts ({call_id}.wav local;
    calls/{call_id}/recording.wav on S3)."""
    stores: list[tuple[ArtifactStore, str]] = [
        (LocalDirStore(_gw_settings.settings.recordings_path), "{call_id}.wav"),
    ]
    if _gw_settings.settings.s3_enabled and _gw_settings.settings.s3_bucket:
        stores.append((S3Store(_gw_settings.settings.s3_bucket,
                               getattr(_gw_settings.settings, "s3_region", "") or ""),
                       "calls/{call_id}/recording.wav"))
    return stores
