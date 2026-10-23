#!/usr/bin/env python3
"""Shared fixtures for the exporter tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.plugins.exporters_helpers import Scenario, new_scenario


@pytest.fixture()
def scenario(tmp_path: Path) -> Scenario:
    """An isolated book/client pair rooted inside ``tmp_path``."""
    return new_scenario(tmp_path)
