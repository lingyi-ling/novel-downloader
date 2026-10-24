#!/usr/bin/env python3
"""Contract tests for :class:`~novel_downloader.schemas.auth.LoginField`."""

from __future__ import annotations

from dataclasses import fields
from typing import get_args

import pytest

from novel_downloader.schemas import LoginField


def test_defaults() -> None:
    field = LoginField(name="username", label="Username", type="text", required=True)

    assert field.default == ""
    assert field.placeholder == ""
    assert field.description == ""


def test_explicit_values() -> None:
    field = LoginField(
        name="cookies",
        label="Cookie",
        type="cookie",
        required=False,
        default="a=b",
        placeholder="Paste cookies",
        description="Login cookies",
    )

    assert field.name == "cookies"
    assert field.label == "Cookie"
    assert field.type == "cookie"
    assert field.required is False
    assert field.default == "a=b"
    assert field.placeholder == "Paste cookies"
    assert field.description == "Login cookies"


def test_required_fields_cannot_be_omitted() -> None:
    with pytest.raises(TypeError):
        LoginField(name="x", label="X", type="text")  # type: ignore[call-arg]


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(TypeError):
        LoginField(  # type: ignore[call-arg]
            name="x",
            label="X",
            type="text",
            required=False,
            unknown=True,
        )


def test_allowed_type_literals() -> None:
    type_field = next(field for field in fields(LoginField) if field.name == "type")

    assert get_args(type_field.type) == ("text", "password", "cookie")


def test_literal_is_not_validated_at_runtime() -> None:
    # ``Literal`` is a static-only constraint; the dataclass stores whatever
    # it is given (plugins are expected to use one of the declared values).
    field = LoginField(
        name="x",
        label="X",
        type="unsupported",  # type: ignore[arg-type]
        required=False,
    )

    assert field.type == "unsupported"


def test_equality_and_repr() -> None:
    first = LoginField(name="u", label="User", type="text", required=True)
    second = LoginField(name="u", label="User", type="text", required=True)

    assert first == second
    assert first != LoginField(name="u", label="User", type="password", required=True)
    assert repr(first).startswith("LoginField(")
    assert "name='u'" in repr(first)
