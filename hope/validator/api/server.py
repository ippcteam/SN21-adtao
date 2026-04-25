"""Validator HTTP API — FastAPI server for miner interaction.

Miners connect here to:
- Fetch episodes for the current epoch
- Submit predictions
- Verify commitments and scoring

All endpoints (except /health) require hotkey signature authentication.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from hope.validator.api.episodes import router as episodes_router
from hope.validator.api.predictions import router as predictions_router
from hope.validator.api.commitments import router as commitments_router
from hope.validator.api.verification import router as verification_router

logger = logging.getLogger(__name__)


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
        title="HOPE SN21 Validator",
        description="Validator HTTP API for the HOPE Impact Prediction Subnet",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Store shared state on the app
    app.state.validator = state

    # Health check (no auth)
    @app.get("/health")
    async def health():
        epoch_id = state.get("current_epoch_id")
        episode_count = len(state.get("episodes", []))
        predictions_count = sum(
            len(preds) for preds in state.get("predictions", {}).values()
        )
        return {
            "status": "ok",
            "service": "hope-sn21-validator",
            "current_epoch": epoch_id,
            "episodes_loaded": episode_count,
            "predictions_received": predictions_count,
        }

    # Register routers
    app.include_router(episodes_router, prefix="/epochs", tags=["episodes"])
    app.include_router(predictions_router, prefix="/epochs", tags=["predictions"])
    app.include_router(commitments_router, prefix="/epochs", tags=["commitments"])
    app.include_router(verification_router, prefix="/epochs", tags=["verification"])

    return app
