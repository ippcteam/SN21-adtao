"""Validator HTTP API — FastAPI server for miner interaction.

Miners connect here to:
- Fetch episodes for the current epoch
- Submit predictions
- Verify commitments and scoring

All endpoints (except /health) require hotkey signature authentication.

DDoS protection:
- Request body size limit (1MB)
- IP-based rate limiting (60 req/min global, 10 req/min on POST)
- Connection limits configured in uvicorn
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from hope.validator.api.episodes import router as episodes_router
from hope.validator.api.predictions import router as predictions_router
from hope.validator.api.commitments import router as commitments_router
from hope.validator.api.verification import router as verification_router
from hope.validator.api.training import router as training_router

logger = logging.getLogger(__name__)

# DDoS protection constants
MAX_REQUEST_BODY_BYTES = 1_048_576  # 1MB — predictions are ~50KB max for 200 episodes
IP_RATE_LIMIT_PER_MINUTE = 120      # Max requests per IP per minute (global)
IP_RATE_LIMIT_POST_PER_MINUTE = 20  # Max POST requests per IP per minute (stricter)
_RATE_WINDOW_SECONDS = 60


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests with bodies larger than MAX_REQUEST_BODY_BYTES.

    Handles both Content-Length header (pre-check) and chunked transfers
    (reads body and checks actual size).
    """

    async def dispatch(self, request: Request, call_next):
        # Pre-check via Content-Length header
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                cl_int = int(content_length)
            except (ValueError, OverflowError):
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Content-Length header"},
                )
            if cl_int > MAX_REQUEST_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"detail": f"Request body too large (max {MAX_REQUEST_BODY_BYTES} bytes)"},
                )

        # For POST/PUT/PATCH without Content-Length (chunked), read and check actual size
        if request.method in ("POST", "PUT", "PATCH") and not content_length:
            body = await request.body()
            if len(body) > MAX_REQUEST_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"detail": f"Request body too large (max {MAX_REQUEST_BODY_BYTES} bytes)"},
                )

        return await call_next(request)


class IPRateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP rate limiting to prevent DDoS."""

    def __init__(self, app):
        super().__init__(app)
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._post_requests: dict[str, list[float]] = defaultdict(list)

    # Cap on unique IPs tracked to prevent unbounded memory growth
    _MAX_TRACKED_IPS = 10_000

    def _clean_old_entries(self, entries: list[float], now: float) -> list[float]:
        cutoff = now - _RATE_WINDOW_SECONDS
        return [t for t in entries if t > cutoff]

    def _evict_stale_ips(self, now: float) -> None:
        """Remove IPs with no recent requests to bound memory."""
        cutoff = now - _RATE_WINDOW_SECONDS * 2
        stale = [ip for ip, times in self._requests.items() if not times or times[-1] < cutoff]
        for ip in stale:
            del self._requests[ip]
            self._post_requests.pop(ip, None)

    async def dispatch(self, request: Request, call_next):
        # Skip rate limiting for health checks
        if request.url.path == "/health":
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.time()

        # Evict stale IPs periodically to bound memory
        if len(self._requests) > self._MAX_TRACKED_IPS:
            self._evict_stale_ips(now)

        # Clean and check global rate
        self._requests[client_ip] = self._clean_old_entries(self._requests[client_ip], now)
        if len(self._requests[client_ip]) >= IP_RATE_LIMIT_PER_MINUTE:
            logger.warning(f"IP rate limit exceeded for {client_ip}")
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Try again later."},
            )
        self._requests[client_ip].append(now)

        # Stricter limit on POST requests
        if request.method == "POST":
            self._post_requests[client_ip] = self._clean_old_entries(
                self._post_requests[client_ip], now,
            )
            if len(self._post_requests[client_ip]) >= IP_RATE_LIMIT_POST_PER_MINUTE:
                logger.warning(f"POST rate limit exceeded for {client_ip}")
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many POST requests. Try again later."},
                )
            self._post_requests[client_ip].append(now)

        return await call_next(request)


def create_app(validator_state: dict | None = None) -> FastAPI:
    """Create the FastAPI application for the validator HTTP API.

    Args:
        validator_state: Shared state dict injected by the validator runner.
            Contains epoch data, predictions, scores, etc.
    """
    state = validator_state if validator_state is not None else {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Validator HTTP API starting")
        yield
        logger.info("Validator HTTP API shutting down")

    app = FastAPI(
        title="AdTAO SN21 Validator",
        description="Validator HTTP API for the AdTAO Impact Prediction Subnet",
        version="0.1.0",
        lifespan=lifespan,
    )

    # DDoS protection middleware (order matters — size check first, then rate limit)
    app.add_middleware(RequestSizeLimitMiddleware)
    app.add_middleware(IPRateLimitMiddleware)

    # Store shared state on the app
    app.state.validator = state

    # Health check (no auth, exempt from IP rate limiting)
    @app.get("/health")
    async def health():
        epoch_id = state.get("current_epoch_id")
        episode_count = len(state.get("episodes", []))
        preds = state.get("predictions", {})
        predictions_count = sum(
            len(p) if isinstance(p, (list, dict)) else 0
            for p in preds.values()
        )
        return {
            "status": "ok",
            "service": "adtao-sn21-validator",
            "current_epoch": epoch_id,
            "episodes_loaded": episode_count,
            "predictions_received": predictions_count,
        }

    # Register routers
    app.include_router(episodes_router, prefix="/v1/epochs", tags=["episodes"])
    app.include_router(predictions_router, prefix="/v1/epochs", tags=["predictions"])
    app.include_router(commitments_router, prefix="/v1/epochs", tags=["commitments"])
    app.include_router(verification_router, prefix="/v1/epochs", tags=["verification"])
    app.include_router(training_router, tags=["training"])

    return app
