"""The voice-gateway process.

    vogon                                           # the console script, or
    uvicorn vogon.gateway_service.main:app --port 8002

Env (layer 1, CONFIGURATION.md): GATEWAY_SERVICE_KEY (empty = auth off, dev
only), GATEWAY_PUBLIC_BASE_URL, GATEWAY_SESSION_STORE=memory|redis,
GATEWAY_TENANT_PROFILES=/path/to/profiles.json. The turn-service binding is
PER CALL (the ``app`` block on POST /v1/tenants/{tenant}/calls) — this process
holds no application DB, no ORM, no control-plane client.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from vogon.gateway.logging_context import CallIdFilter
from vogon.gateway.metrics.sink import metrics_sink
from vogon.gateway.settings import settings
from vogon.gateway_service import routes
from vogon.gateway_service.capabilities import enforce
from vogon.gateway_service.config import gateway_config

logger = logging.getLogger(__name__)


def _configure_logging(level_name: str = "INFO") -> None:
    """Stand the gateway's logging up, defensively.

    Matches the application's format exactly — one service split into two,
    read from one journal, so `[name] [call_id]` has to line up or a call
    cannot be followed across the seam by eye or by grep.

    THE LEVEL IS SET ON `app` EXPLICITLY, not just via basicConfig. Under
    plain uvicorn basicConfig does install the root handler (uvicorn
    configures its own named loggers and leaves root alone, verified), but
    it is a NO-OP the moment anything has already put a handler on root — a
    different runner, an embedding app, a stray basicConfig in an import.
    Root then keeps its WARNING default and every vogon.gateway INFO line
    disappears with nothing to show it ever tried. The application carries
    the same guard for the same reason; the gateway once shipped without it.
    """
    level = getattr(logging, (level_name or "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] [%(call_id)s] %(message)s",
    )
    logging.getLogger("vogon").setLevel(level)
    _filter = CallIdFilter()
    for handler in logging.root.handlers:
        handler.addFilter(_filter)
    # Uvicorn's own handlers never saw the filter, so any record they format
    # against a call_id-bearing format string raised a formatting error.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            handler.addFilter(_filter)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    cfg = gateway_config()
    _configure_logging(cfg.log_level)
    # D5 capability gate: every native audio capability logs one line;
    # missing + policy=refuse → the process does not come up degraded.
    app.state.capabilities = enforce(cfg.capability_policy)
    # Respect an already-injected store (tests, embedding apps): the
    # lifespan only fills the default.
    if routes._store is None:
        routes.use_store(await routes.default_store_from_config(cfg))
    # the gateway emits latency/TTS/barge metrics through the shared sink: this process must
    # start it (it never did — every gateway metric was dropped), on the same storage base the
    # dashboard reads (STORAGE_BASE_PATH, as the turn service)
    metrics_sink.start(settings.storage_base_path, service="voice-gateway")
    logger.info(
        "voice gateway up | store=%s auth=%s public_base=%s",
        cfg.session_store, "on" if cfg.auth_enabled else "OFF (dev)",
        cfg.public_base_url or "(request-derived)",
    )
    yield
    await metrics_sink.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="Codelexin Voice Gateway", lifespan=_lifespan)
    app.include_router(routes.router)

    @app.get("/health")
    async def health():
        caps = getattr(app.state, "capabilities", [])
        return {"ok": True, "service": "voice-gateway",
                "capabilities": {c.name: c.ok for c in caps}}

    return app


app = create_app()


def cli() -> None:
    """``vogon`` — serve the gateway. GATEWAY_HOST / GATEWAY_PORT pick the bind
    address (0.0.0.0:8002 by default); everything else is in the environment or
    the .env file (CONFIGURATION.md)."""
    import os  # noqa: PLC0415 — only the console entry point needs these

    import uvicorn  # noqa: PLC0415
    uvicorn.run("vogon.gateway_service.main:app", host=os.environ.get("GATEWAY_HOST", "0.0.0.0"),
                port=int(os.environ.get("GATEWAY_PORT", "8002")), log_level="info")
