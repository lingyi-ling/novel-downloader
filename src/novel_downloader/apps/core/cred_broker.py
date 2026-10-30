#!/usr/bin/env python3
"""
novel_downloader.apps.core.cred_broker
----------------------------------------------

In-memory credential request broker
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from novel_downloader.schemas import LoginField

from .models import CredRequest

# wait time for credentials before timing out (seconds)
REQUEST_TIMEOUT: int = 120
# Per-claim lease time (seconds)
CLAIM_TTL: int = 15

# Global request store
_CRED_LOCK = asyncio.Lock()
_CRED_REQS: dict[str, CredRequest] = {}  # req_id -> CredRequest


async def create_cred_request(
    *,
    task_id: str,
    title: str,
    fields: list[LoginField],
    prefill: dict[str, str] | None = None,
) -> CredRequest:
    """
    Create and register a new credential request for a task.
    """
    async with _CRED_LOCK:
        req = CredRequest(
            task_id=task_id,
            title=title,
            fields=list(fields),
            prefill=prefill or {},
        )
        _CRED_REQS[req.req_id] = req
        return req


async def claim_next_request(client_id: str) -> CredRequest | None:
    """
    Claim the next pending unclaimed request; also releases expired claims.
    """
    now = time.monotonic()
    async with _CRED_LOCK:
        # release stale claims
        for r in _CRED_REQS.values():
            if (
                (not r.done)
                and r.claimed_by
                and r.claimed_at
                and (now - r.claimed_at) > CLAIM_TTL
            ):
                r.claimed_by = None
                r.claimed_at = None
        # claim one
        for r in _CRED_REQS.values():
            if not r.done and r.claimed_by is None:
                r.claimed_by = client_id
                r.claimed_at = now
                return r
    return None


async def refresh_claim(req_id: str, client_id: str) -> None:
    """
    Extend the claim lease for a request if it is still owned by the client.
    """
    now = time.monotonic()
    async with _CRED_LOCK:
        r = _CRED_REQS.get(req_id)
        if r and (not r.done) and r.claimed_by == client_id:
            r.claimed_at = now


async def complete_request(req_id: str, result: dict[str, str] | None) -> None:
    """
    Resolve a request with credentials (or None for cancel/timeout) and wake waiters.
    """
    async with _CRED_LOCK:
        r = _CRED_REQS.get(req_id)
        if not r or r.done:
            return
        r.result = result
        r.done = True
        r.event.set()


async def get_req_state(req_id: str) -> tuple[bool, bool]:
    """
    Return (exists, done) for a request id.
    """
    async with _CRED_LOCK:
        r = _CRED_REQS.get(req_id)
        if not r:
            return False, False
        return True, r.done


def request_to_dict(req: CredRequest) -> dict[str, Any]:
    """
    Serialise a credential request for the JSON API.

    ``label`` / ``placeholder`` / ``description`` are passed through verbatim:
    they come from the site plugins and are not translated here; clients
    decide how to display them.
    """
    return {
        "req_id": req.req_id,
        "task_id": req.task_id,
        "title": req.title,
        "fields": [
            {
                "name": field.name,
                "label": field.label,
                "type": field.type,
                "required": field.required,
                "default": field.default,
                "placeholder": field.placeholder,
                "description": field.description,
            }
            for field in req.fields
        ],
        "prefill": dict(req.prefill),
        "done": req.done,
    }


async def list_requests() -> list[dict[str, Any]]:
    """
    Snapshot the pending (not yet completed) credential requests.

    Both the filtering and the serialisation happen inside the broker lock, so
    concurrent ``create_cred_request`` / ``complete_request`` calls on the same
    loop can never produce a torn view. The claim state is left untouched and
    done requests are filtered out.
    """
    async with _CRED_LOCK:
        return [request_to_dict(r) for r in _CRED_REQS.values() if not r.done]


def cleanup_request(req_id: str) -> None:
    """
    Remove a request from the broker (call after the task consumes the result).
    """
    _CRED_REQS.pop(req_id, None)
