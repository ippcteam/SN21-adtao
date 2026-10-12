"""validator.adtao.io sends daily-results requests to the public feed.

WHY (9 Oct 2026, decided by Khurram). The validator site served /v1/daily/*
from its own ledger root, which on that host is empty: the daily documents are
written on the model executor and published to the public feed
(hope-bittensor-api). So the validator site answered "nothing published" for
days the public feed had published, and a miner checking the wrong address
would think we hide results. The two addresses must never disagree.

When SN21_DAILY_FEED_URL is set, every /v1/daily/* request is redirected (307,
same path and query) to that feed instead of being answered from the local
ledger. Unset, the local router serves as before (a validator that keeps its
own ledger).
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Request
from starlette.responses import RedirectResponse

FEED_URL_ENV = "SN21_DAILY_FEED_URL"

router = APIRouter()


def feed_url(environ=os.environ) -> str:
    return (environ.get(FEED_URL_ENV) or "").strip().rstrip("/")


@router.get("/{rest:path}")
def to_public_feed(rest: str, request: Request):
    target = f"{feed_url()}/v1/daily/{rest}"
    if request.url.query:
        target += f"?{request.url.query}"
    return RedirectResponse(target, status_code=307)
