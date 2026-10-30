#!/usr/bin/env python3
"""
tests.core.test_cred_broker_list
--------------------------------

Offline tests for the credential broker's listing API
(:func:`novel_downloader.apps.core.cred_broker.list_requests`), which the
REST layer consumes for ``GET /api/v1/login/requests``.

Covered:

* empty broker yields an empty list
* the snapshot carries the exact field shape the API used to pass through
  (``req_id`` / ``task_id`` / ``title`` / ``fields`` / ``prefill`` / ``done``,
  field keys in the same order)
* done requests are filtered out
* the returned snapshot is detached from broker state
* concurrent create / complete / list on one event loop never crashes or
  produces a torn item
* the broker's serialisation matches the API layer's passthrough serialiser
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from novel_downloader.apps.api.routes import cred_request_to_dict
from novel_downloader.apps.core import cred_broker
from novel_downloader.apps.core.cred_broker import (
    complete_request,
    create_cred_request,
    list_requests,
)
from novel_downloader.apps.core.models import CredRequest
from novel_downloader.schemas import LoginField

#: Top-level keys of a listed request, in JSON insertion order.
TOP_LEVEL_KEYS = ["req_id", "task_id", "title", "fields", "prefill", "done"]

#: Keys of one listed field, in JSON insertion order.
FIELD_KEYS = [
    "name",
    "label",
    "type",
    "required",
    "default",
    "placeholder",
    "description",
]


@pytest.fixture(autouse=True)
def _fresh_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Give every test a pristine credential broker.

    The broker keeps a module-global request store and an ``asyncio.Lock``
    created at import time; a contended lock binds itself to the first event
    loop that queues on it, which would poison later tests on a fresh loop.
    """
    monkeypatch.setattr(cred_broker, "_CRED_LOCK", asyncio.Lock())
    monkeypatch.setattr(cred_broker, "_CRED_REQS", {})


def _field(name: str, type_: str = "text", **kwargs: Any) -> LoginField:
    return LoginField(
        name=name,
        label=kwargs.pop("label", f"{name}.label"),
        type=type_,  # type: ignore[arg-type]
        required=kwargs.pop("required", False),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_requests_empty() -> None:
    assert await list_requests() == []


@pytest.mark.asyncio
async def test_list_requests_snapshot_shape() -> None:
    """Listed items keep the exact shape (and key order) the API exposes."""
    req = await create_cred_request(
        task_id="task-1",
        title="斗破苍穹",
        fields=[
            _field(
                "username",
                required=True,
                default="admin",
                placeholder="user@example.com",
                description="login.username.description",
            ),
            _field("password", "password", required=True),
        ],
        prefill={"username": "admin"},
    )
    await create_cred_request(task_id="task-2", title="Other", fields=[])

    snapshot = await list_requests()

    assert len(snapshot) == 2
    assert list(snapshot[0]) == TOP_LEVEL_KEYS
    assert list(snapshot[0]["fields"][0]) == FIELD_KEYS
    assert snapshot[0] == {
        "req_id": req.req_id,
        "task_id": "task-1",
        "title": "斗破苍穹",
        "fields": [
            {
                "name": "username",
                "label": "username.label",
                "type": "text",
                "required": True,
                "default": "admin",
                "placeholder": "user@example.com",
                "description": "login.username.description",
            },
            {
                "name": "password",
                "label": "password.label",
                "type": "password",
                "required": True,
                "default": "",
                "placeholder": "",
                "description": "",
            },
        ],
        "prefill": {"username": "admin"},
        "done": False,
    }
    assert snapshot[1] == {
        "req_id": snapshot[1]["req_id"],
        "task_id": "task-2",
        "title": "Other",
        "fields": [],
        "prefill": {},
        "done": False,
    }


@pytest.mark.asyncio
async def test_list_requests_matches_api_passthrough() -> None:
    """Every listed item serialises identically to the API layer's own."""
    req = await create_cred_request(
        task_id="t1",
        title="Book",
        fields=[_field("username", required=True), _field("cookies", "cookie")],
        prefill={"username": "u"},
    )

    snapshot = await list_requests()

    assert snapshot == [cred_request_to_dict(req)]
    # ``json.dumps`` is order-sensitive, so this also pins the JSON byte order.
    assert json.dumps(snapshot, ensure_ascii=False) == json.dumps(
        [cred_request_to_dict(req)], ensure_ascii=False
    )


# ---------------------------------------------------------------------------
# Filtering / snapshot semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_requests_filters_done() -> None:
    a = await create_cred_request(task_id="t1", title="A", fields=[])
    b = await create_cred_request(task_id="t2", title="B", fields=[])
    await create_cred_request(task_id="t3", title="C", fields=[])
    await complete_request(b.req_id, None)

    snapshot = await list_requests()

    assert [item["req_id"] for item in snapshot] == [a.req_id, snapshot[1]["req_id"]]
    assert {item["task_id"] for item in snapshot} == {"t1", "t3"}
    assert all(item["done"] is False for item in snapshot)


@pytest.mark.asyncio
async def test_list_requests_snapshot_is_detached() -> None:
    """Mutating the snapshot (or completing a request afterwards) is safe."""
    req = await create_cred_request(
        task_id="t1",
        title="Book",
        fields=[_field("username")],
        prefill={"username": "u"},
    )
    snapshot = await list_requests()
    assert len(snapshot) == 1

    # Mutate the returned data: the broker is unaffected.
    snapshot[0]["title"] = "mutated"
    snapshot[0]["prefill"]["injected"] = "x"
    snapshot[0]["fields"][0]["label"] = "mutated"

    # Complete the request afterwards: the old snapshot is not rewritten.
    await complete_request(req.req_id, {"username": "pw"})

    assert snapshot[0]["title"] == "mutated"
    assert snapshot[0]["prefill"] == {"username": "u", "injected": "x"}
    assert snapshot[0]["done"] is False

    again = await list_requests()
    assert again == []  # done requests are filtered out


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_requests_survives_concurrent_create_and_complete() -> None:
    """
    Interleaved create / complete / list on one event loop never crashes and
    never yields a torn item.
    """
    total = 30

    async def creator(i: int) -> CredRequest:
        return await create_cred_request(
            task_id=f"task-{i}",
            title=f"Book {i}",
            fields=[_field(f"field{i}", required=True)],
            prefill={"field0": "v"},
        )

    async def listener() -> None:
        for _ in range(25):
            items = await list_requests()
            for item in items:
                assert list(item) == TOP_LEVEL_KEYS
                assert item["done"] is False
            await asyncio.sleep(0)

    first_batch = await asyncio.gather(*(creator(i) for i in range(total // 2)))

    await asyncio.gather(
        *(creator(i) for i in range(total // 2, total)),
        *(listener() for _ in range(4)),
        *(complete_request(r.req_id, None) for r in first_batch[::2]),
    )

    done_ids = {r.req_id for r in first_batch[::2]}
    final = await list_requests()
    assert len(final) == total - len(done_ids)
    assert all(item["req_id"] not in done_ids for item in final)
