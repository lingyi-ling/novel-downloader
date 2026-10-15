#!/usr/bin/env python3
"""
novel_downloader.plugins.rule
-----------------------------

Rule-driven ("book source") plugin system.

Instead of writing a Python class per website, a source is described by a
small JSON document (see :mod:`~.model`) kept in one of:

* ``novel_downloader/plugins/rule/builtin/*.json`` -- shipped with the project
* ``<cwd>/rules/*.json``                          -- project-local sources
* ``$NOVEL_RULES_DIR/*.json``                     -- custom directory
* ``<user-config>/rules/*.json``                  -- per-user sources

This module glues those rules into the existing plugin registry, so a
rule-based source behaves exactly like a hand-written one: it shows up in
``search``, can be selected with ``--site`` and can be resolved from a URL.
"""

from __future__ import annotations

import json
import logging
import os
import re
from importlib.resources import files
from pathlib import Path
from typing import Any

from novel_downloader.plugins.registry import registrar

from .fetcher import RuleFetcher
from .model import (
    Field,
    Rule,
    RuleError,
    parse_rule,
)
from .parser import RuleParser
from .searcher import RuleSearcher

logger = logging.getLogger(__name__)

__all__ = [
    "Rule",
    "RuleError",
    "Field",
    "load_rules",
    "reload_rules",
    "iter_rules",
    "ensure_registered",
    "register_all",
    "resolve_url",
]

_BUILTIN_PKG = "novel_downloader.plugins.rule.builtin"

_rules: dict[str, Rule] = {}
_meta: dict[str, dict[str, str]] = {}  # site_key -> {"origin": ..., "file": ...}
_host_map: dict[str, str] = {}
_registered: set[str] = set()
_loaded = False


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
def _user_dirs() -> list[Path]:
    dirs: list[Path] = [Path.cwd() / "rules"]
    env = os.environ.get("NOVEL_RULES_DIR")
    if env:
        dirs.append(Path(env).expanduser())
    try:
        from platformdirs import user_config_path

        dirs.append(user_config_path("novel_downloader") / "rules")
    except Exception:  # pragma: no cover - platformdirs is a hard dependency
        pass
    return dirs


def _normalize_key(site_key: str) -> str:
    key = site_key.strip().lower()
    if key and key[0].isdigit():
        return f"n{key}"
    return key


def _load_payload(text: str, origin: str, filename: str) -> None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("Skip rule '%s' (%s): invalid JSON: %s", filename, origin, e)
        return

    try:
        rule = parse_rule(data, source=filename)
    except RuleError as e:
        logger.warning("Skip rule '%s' (%s): %s", filename, origin, e)
        return

    key = _normalize_key(rule.site_key)
    if key in _rules and origin == "builtin":
        logger.warning("Duplicate builtin rule '%s'; keeping the first", key)
        return

    if key in _rules:
        logger.info("Rule source '%s' overridden by %s rule", key, origin)
    _rules[key] = rule
    _meta[key] = {"origin": origin, "file": filename}
    for host in rule.hosts:
        _host_map[host] = key


def _is_ignored(filename: str) -> bool:
    """Rule files starting with ``_`` are templates/drafts and never loaded."""
    return filename.startswith("_")


def load_rules(*, force: bool = False) -> dict[str, Rule]:
    """Discover and parse every available rule file (idempotent)."""
    global _loaded
    if _loaded and not force:
        return _rules

    _rules.clear()
    _meta.clear()
    _host_map.clear()
    _registered.clear()

    try:
        builtin = files(_BUILTIN_PKG)
        for entry in sorted(builtin.iterdir(), key=lambda e: e.name):
            if entry.name.endswith(".json"):
                _load_payload(
                    entry.read_text(encoding="utf-8-sig"), "builtin", entry.name
                )
    except Exception as e:  # pragma: no cover - packaging edge
        logger.warning("Could not load builtin rules: %s", e)

    for directory in _user_dirs():
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            if _is_ignored(path.name):
                continue
            try:
                _load_payload(path.read_text(encoding="utf-8-sig"), "user", str(path))
            except OSError as e:
                logger.warning("Could not read rule file %s: %s", path, e)

    _loaded = True
    logger.debug("Loaded %d rule-based source(s)", len(_rules))
    return _rules


def reload_rules() -> dict[str, Rule]:
    """Force a re-scan of rule files (mainly useful for the CLI)."""
    return load_rules(force=True)


def iter_rules() -> list[tuple[str, Rule, dict[str, str]]]:
    """Return ``(site_key, rule, meta)`` tuples for every loaded rule."""
    load_rules()
    return [(key, rule, _meta.get(key, {})) for key, rule in _rules.items()]


# --------------------------------------------------------------------------- #
# registration into the plugin registry
# --------------------------------------------------------------------------- #
def _make_fetcher(rule: Rule, key: str) -> type[RuleFetcher]:
    return type(
        f"RuleFetcher_{key}",
        (RuleFetcher,),
        {"_rule": rule, "site_name": key, "DEFAULT_BASE_URL": rule.base_url},
    )


def _make_parser(rule: Rule, key: str) -> type[RuleParser]:
    return type(
        f"RuleParser_{key}",
        (RuleParser,),
        {"_rule": rule, "site_name": key},
    )


def _make_searcher(rule: Rule, key: str) -> type[RuleSearcher]:
    priority = rule.search.priority if rule.search else rule.priority
    return type(
        f"RuleSearcher_{key}",
        (RuleSearcher,),
        {
            "_rule": rule,
            "site_name": key,
            "BASE_URL": rule.base_url,
            "priority": priority,
        },
    )


def _register(key: str, rule: Rule) -> None:
    registrar.register_site(
        key,
        fetcher=_make_fetcher(rule, key),
        parser=_make_parser(rule, key),
        searcher=_make_searcher(rule, key) if rule.search is not None else None,
    )
    _registered.add(key)


def ensure_registered(site_key: str) -> bool:
    """
    Register one rule-based source on demand.

    :return: True if a rule exists for ``site_key`` (registered or already so).
    """
    key = _normalize_key(site_key)
    load_rules()
    if key not in _rules:
        return False
    if key not in _registered:
        _register(key, _rules[key])
    return True


def register_all() -> None:
    """Register every loaded rule-based source."""
    load_rules()
    for key, rule in _rules.items():
        if key not in _registered:
            _register(key, rule)


# --------------------------------------------------------------------------- #
# url -> source resolution
# --------------------------------------------------------------------------- #
def resolve_url(host: str, path: str, query: str = "") -> dict[str, Any] | None:
    """
    Resolve a URL to a rule-based source.

    :return: a ``BookURLInfo``-shaped dict, or None when no rule matches.
    """
    load_rules()
    key = _host_map.get(host.lower())
    if key is None:
        return None

    rule = _rules[key]
    resolve = rule.resolve
    if resolve is None:
        return None

    if resolve.chapter and (m := re.search(resolve.chapter, path)):
        groups = m.groups()
        return {
            "site_key": key,
            "book_id": groups[0] if groups else None,
            "chapter_id": groups[1] if len(groups) > 1 else None,
        }

    if resolve.book and (m := re.search(resolve.book, path)):
        groups = m.groups()
        return {
            "site_key": key,
            "book_id": groups[0] if groups else None,
            "chapter_id": None,
        }

    return None
