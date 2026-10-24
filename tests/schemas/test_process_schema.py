#!/usr/bin/env python3
"""Contract tests for the pipeline metadata models in ``schemas.process``."""

from __future__ import annotations

import json
from typing import get_args, get_type_hints

from novel_downloader.schemas import ExecutedStageMeta, PipelineMeta


class TestExecutedStageMeta:
    def test_required_keys(self) -> None:
        assert ExecutedStageMeta.__required_keys__ == frozenset(
            {"processed_at", "depends_on", "config_hash"}
        )
        assert ExecutedStageMeta.__optional_keys__ == frozenset()

    def test_type_hints(self) -> None:
        hints = get_type_hints(ExecutedStageMeta)

        assert hints["processed_at"] is str
        assert getattr(hints["depends_on"], "__origin__", None) is list
        assert get_args(hints["depends_on"]) == (str,)
        assert hints["config_hash"] is str

    def test_construction(self) -> None:
        meta = ExecutedStageMeta(
            processed_at="2026-01-01T00:00:00Z",
            depends_on=["cleaner"],
            config_hash="abc123",
        )

        assert meta["depends_on"] == ["cleaner"]


class TestPipelineMeta:
    def test_required_keys(self) -> None:
        assert PipelineMeta.__required_keys__ == frozenset({"pipeline", "executed"})
        assert PipelineMeta.__optional_keys__ == frozenset()

    def test_type_hints(self) -> None:
        hints = get_type_hints(PipelineMeta)

        assert getattr(hints["pipeline"], "__origin__", None) is list
        assert get_args(hints["pipeline"]) == (str,)
        assert getattr(hints["executed"], "__origin__", None) is dict

    def test_nested_structure(self) -> None:
        meta = PipelineMeta(
            pipeline=["cleaner", "corrector"],
            executed={
                "cleaner": ExecutedStageMeta(
                    processed_at="2026-01-01T00:00:00Z",
                    depends_on=[],
                    config_hash="h1",
                ),
                "corrector": ExecutedStageMeta(
                    processed_at="2026-01-01T00:01:00Z",
                    depends_on=["cleaner"],
                    config_hash="h2",
                ),
            },
        )

        assert meta["pipeline"] == ["cleaner", "corrector"]
        assert meta["executed"]["corrector"]["depends_on"] == ["cleaner"]

    def test_empty_default_shape(self) -> None:
        # This is the shape returned by ``_load_pipeline_meta`` for a missing
        # or unreadable ``pipeline.json``.
        meta = PipelineMeta(pipeline=[], executed={})

        assert meta == {"pipeline": [], "executed": {}}

    def test_unknown_fields_are_tolerated(self) -> None:
        meta = PipelineMeta(pipeline=[], executed={}, version=2)

        assert meta["version"] == 2

    def test_json_round_trip(self) -> None:
        meta = PipelineMeta(
            pipeline=["cleaner"],
            executed={
                "cleaner": ExecutedStageMeta(
                    processed_at="2026-01-01T00:00:00Z",
                    depends_on=[],
                    config_hash="h1",
                )
            },
        )

        restored = json.loads(json.dumps(meta))

        assert restored == meta
        assert restored["executed"]["cleaner"]["config_hash"] == "h1"
