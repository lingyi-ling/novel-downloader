#!/usr/bin/env python3
"""
novel_downloader.infra.persistence.state
----------------------------------------

State management for user preferences and runtime flags.
"""

__all__ = ["StateManager", "state_mgr"]

import contextlib
import json
import os
from pathlib import Path
from typing import Any

from novel_downloader.infra.paths import STATE_PATH


class StateManager:
    """
    Manages persistent state for user preferences and runtime flags.
    """

    def __init__(self, path: Path = STATE_PATH) -> None:
        self._path = path
        self._data = self._load()

    def get_language(self) -> str:
        """
        Load the user's language preference.

        :return: Language code string
        """
        return self._data.get("lang") or "zh_CN"

    def set_language(self, lang: str) -> None:
        """
        Save the user's language preference.

        :param lang: Language code
        """
        self._data["lang"] = lang
        self._save()

    def _load(self) -> dict[str, Any]:
        """
        Load the configuration file into a Python dictionary.

        A file that cannot be parsed, or that holds valid JSON which is not a
        mapping, is unusable as state: it is quarantined (renamed to
        ``<name>.corrupt``) so the original bytes stay recoverable, and an
        empty state is returned.

        :return: A dict representing the full config state.
        """
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            self._quarantine_corrupt()
            return {}
        if not isinstance(data, dict):
            self._quarantine_corrupt()
            return {}
        return data

    def _quarantine_corrupt(self) -> None:
        """
        Move an unreadable state file aside, best-effort.

        Without this, the next save silently overwrites the only copy of the
        (possibly hand-edited or partially written) file.
        """
        with contextlib.suppress(OSError):
            os.replace(self._path, self._path.with_name(self._path.name + ".corrupt"))

    def _save(self) -> None:
        """
        Save a configuration dictionary to the config file.

        The payload is written to a sibling temporary file and moved into
        place with :func:`os.replace`, so a crash mid-write can never leave a
        truncated state file behind.

        :param data: A dict representing the full config state to be written.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(self._data, ensure_ascii=False, indent=2)
        tmp_path = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(content, encoding="utf-8")
        try:
            os.replace(tmp_path, self._path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            raise


state_mgr = StateManager()
