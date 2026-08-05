"""Validator HTTP API — FastAPI server for miner interaction.

Miners connect here to:
- Fetch episodes for the current epoch
- Submit predictions
- Verify commitments and scoring

All endpoints (except /health) require hotkey signature authentication.

DDoS protection:
- Request body size limit (1MB)
- IP-based rate limiting (120 req/min global, 20 req/min on POST)
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

from hope.validator.api.commitments import router as commitments_router
from hope.validator.api.daily import router as daily_router
from hope.validator.api.episodes import router as episodes_router
from hope.validator.api.predictions import router as predictions_router
from hope.validator.api.registration import router as registration_router
from hope.validator.api.training import router as training_router
from hope.validator.api.verification import router as verification_router

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
        # If the caller stashed a metagraph refresh coroutine in state,
        # spin it up as a background task for the API's lifetime so
        # newly-registered miners become visible without restarting.
        refresh_task = None
        refresh_fn = state.get("_metagraph_refresh_coro")
        if refresh_fn is not None:
            import asyncio
            refresh_task = asyncio.create_task(refresh_fn(state))
        yield
        if refresh_task is not None:
            refresh_task.cancel()
            try:
                await refresh_task
            except BaseException:
                pass
        logger.info("Validator HTTP API shutting down")

    app = FastAPI(
        title="SN21 Validator",
        description="Validator HTTP API for the Impact Prediction Subnet",
        version="0.1.0",
        lifespan=lifespan,
    )

    # DDoS protection middleware (order matters — size check first, then rate limit)
    app.add_middleware(RequestSizeLimitMiddleware)
    app.add_middleware(IPRateLimitMiddleware)

    # Store shared state on the app
    app.state.validator = state

    # Root: friendly 200 for health probes hitting `/`. The actual API
    # endpoints live under `/v1/*` and `/health`. The package version is
    # echoed back so operators can `curl /` and immediately tell which
    # build is live without needing log access — useful when chasing
    # a memory issue across multiple deploys.
    try:
        from importlib.metadata import version as _pkg_version
        _SN21_VERSION = _pkg_version("hope-sn21")
    except Exception:
        _SN21_VERSION = "unknown"

    @app.get("/")
    async def root():
        return {
            "service": "sn21-validator",
            "version": _SN21_VERSION,
            "endpoints": {
                "health": "/health",
                "api": "/v1/epochs/{epoch_id}/...",
                "debug": "/debug/memory",
            },
        }

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
        # Surface the submission deadline + open/closed flag so miners
        # can pre-check whether the window is still open before doing
        # the work of generating predictions. Wall-clock deadline (ISO
        # UTC string) — the same value the /predictions endpoint
        # checks against.
        from datetime import datetime as _dt
        from datetime import timezone as _tz
        deadline_str = state.get("deadline")
        submission_open_flag = bool(state.get("submission_open", False))
        seconds_until_deadline: int | None = None
        if deadline_str:
            try:
                deadline_dt = _dt.fromisoformat(deadline_str)
                seconds_until_deadline = int(
                    (deadline_dt - _dt.now(_tz.utc)).total_seconds()
                )
            except (TypeError, ValueError):
                seconds_until_deadline = None
        return {
            "status": "ok",
            "service": "sn21-validator",
            "current_epoch": epoch_id,
            "episodes_loaded": episode_count,
            "predictions_received": predictions_count,
            "deadline_utc": deadline_str,
            "submission_open": submission_open_flag,
            "seconds_until_deadline": seconds_until_deadline,
        }

    # Register routers
    app.include_router(episodes_router, prefix="/v1/epochs", tags=["episodes"])
    app.include_router(predictions_router, prefix="/v1/epochs", tags=["predictions"])
    app.include_router(commitments_router, prefix="/v1/epochs", tags=["commitments"])
    app.include_router(verification_router, prefix="/v1/epochs", tags=["verification"])
    # Daily verifiability: receipts + anchored accuracy docs + the chain
    # walk. Public and unauthenticated BY DESIGN — every document served
    # is attested and anchored, so gating it would add no security and
    # would break the one property it exists for: anyone can check us.
    app.include_router(daily_router, prefix="/v1/daily", tags=["daily"])
    app.include_router(training_router, tags=["training"])
    app.include_router(registration_router, prefix="/v1", tags=["registration"])

    # Debug endpoints — always registered so a successful deploy is
    # observable via curl, independent of which env vars happened to make
    # it through to the running process. /debug/memory works even when
    # tracemalloc isn't enabled (just returns RSS + GC stats from /proc
    # and gc, both cheap). /debug/tracemalloc returns an explicit error
    # when tracemalloc isn't active rather than 404'ing the route.
    # Non-sensitive (file:line statistics only); operator-curl from any IP.
    import gc as _gc
    import os as _os
    import tracemalloc as _tm

    def _read_proc_status_kb(field: str) -> int | None:
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith(f"{field}:"):
                        return int(line.split()[1])
        except Exception:
            return None
        return None

    @app.get("/debug/memory")
    async def debug_memory():
        """Cheap process-memory snapshot. Always safe to call."""
        current, peak = _tm.get_traced_memory() if _tm.is_tracing() else (0, 0)
        return {
            "version": _SN21_VERSION,
            "pid": _os.getpid(),
            "rss_kb": _read_proc_status_kb("VmRSS"),
            "vmsize_kb": _read_proc_status_kb("VmSize"),
            "tracemalloc_enabled": _tm.is_tracing(),
            "tracemalloc_traced_bytes": current,
            "tracemalloc_peak_bytes": peak,
            "gc_counts": _gc.get_count(),
            "gc_threshold": _gc.get_threshold(),
            "gc_objects": len(_gc.get_objects()),
            "env_SN21_TRACEMALLOC": _os.environ.get("SN21_TRACEMALLOC", "<unset>"),
        }

    @app.get("/debug/tracemalloc")
    async def debug_tracemalloc(limit: int = 20, group_by: str = "lineno"):
        """Top allocation sites by current tracked size.

        Args:
            limit: number of top sites to return (default 20).
            group_by: 'lineno' for per-line stats (concise), 'traceback'
                for full stack-trace clustering (verbose; useful when
                the leaking allocation site has multiple callers).
        """
        if not _tm.is_tracing():
            return {"error": "tracemalloc not enabled", "hint": "set SN21_TRACEMALLOC=1 and restart"}
        snap = _tm.take_snapshot()
        snap = snap.filter_traces((
            _tm.Filter(False, "<frozen importlib._bootstrap>"),
            _tm.Filter(False, "<frozen importlib._bootstrap_external>"),
            _tm.Filter(False, _tm.__file__),
        ))
        stats = snap.statistics(group_by if group_by in ("lineno", "traceback", "filename") else "lineno")
        out = []
        for stat in stats[:limit]:
            tb_lines = list(stat.traceback.format()) if group_by == "traceback" else None
            out.append({
                "size_kb": round(stat.size / 1024, 1),
                "count": stat.count,
                "avg_bytes": stat.size // max(1, stat.count),
                "location": str(stat.traceback[0]),
                **({"traceback": tb_lines} if tb_lines else {}),
            })
        current, peak = _tm.get_traced_memory()
        return {
            "tracemalloc_traced_mb": round(current / 1024 / 1024, 1),
            "tracemalloc_peak_mb": round(peak / 1024 / 1024, 1),
            "top": out,
        }

    return app
