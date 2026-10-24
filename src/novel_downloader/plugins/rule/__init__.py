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

import contextlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from importlib.resources import files
from pathlib import Path
from typing import Any, TypedDict

from novel_downloader.infra.i18n import t
from novel_downloader.plugins.registry import registrar

from .fetcher import RuleFetcher
from .model import (
    Field,
    Rule,
    RuleError,
    parse_rule,
)
from .parser import RuleParser
from .remote import fetch_rule
from .searcher import RuleSearcher

logger = logging.getLogger(__name__)

__all__ = [
    "Rule",
    "RuleError",
    "Field",
    "RefreshResult",
    "fetch_rule",
    "load_rules",
    "reload_rules",
    "refresh_rules",
    "iter_rules",
    "ensure_registered",
    "register_all",
    "resolve_url",
    "install_dir",
    "install_rule",
    "remove_rule",
]


class RefreshResult(TypedDict):
    """Outcome of :func:`refresh_rules`."""

    added: list[str]
    updated: list[str]
    failed: dict[str, str]


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
# installing / removing user rules
# --------------------------------------------------------------------------- #
_SAFE_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1F\x7F]')
_WIN_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_MAX_RULE_STEM = 100


def install_dir() -> Path:
    """
    Directory new user rule files are installed into.

    ``$NOVEL_RULES_DIR`` when set, otherwise the per-user config directory,
    which is also the last directory :func:`load_rules` scans.
    """
    env = os.environ.get("NOVEL_RULES_DIR")
    if env:
        return Path(env).expanduser()
    return _user_dirs()[-1]


def _safe_rule_filename(name: str, *, fallback: str = "rule") -> str:
    """
    Turn an arbitrary ``name`` into a single, portable ``*.json`` filename.

    Directory components (both ``/`` and ``\\``), Windows-reserved characters,
    control characters, reserved device names and leading dots/underscores are
    removed so the result can never escape :func:`install_dir` and always gets
    loaded again (files starting with ``_`` are ignored by the loader).
    """
    raw = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    if raw.lower().endswith(".json"):
        raw = raw[:-5]

    stem = _SAFE_NAME_RE.sub("_", raw).strip(" .").lstrip("_")
    if not stem:
        stem = _SAFE_NAME_RE.sub("_", str(fallback)).strip(" .").lstrip("_")
    if not stem:
        stem = "rule"

    if stem.split(".", 1)[0].strip(" .").upper() in _WIN_RESERVED_NAMES:
        stem = f"rule_{stem}"

    if len(stem) > _MAX_RULE_STEM:
        stem = stem[:_MAX_RULE_STEM].rstrip(" ._") or "rule"

    return f"{stem}.json"


def _serialize_rule(data: Mapping[str, Any]) -> str:
    """Serialize a rule mapping the same way a hand-written file would look."""
    try:
        text = json.dumps(dict(data), ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as e:
        raise RuleError(f"rule is not JSON-serializable: {e}") from e
    return text + "\n"


def _load_json_text(text: str, source: str) -> dict[str, Any]:
    """Parse rule text, requiring a JSON object as the document root."""
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuleError(
            t("{path}: invalid JSON: {err}").format(path=source, err=e)
        ) from e
    if not isinstance(data, dict):
        raise RuleError(t("{source}: rule must be a JSON object").format(source=source))
    return data


def install_rule(
    data: Mapping[str, Any] | str | Path,
    name: str | None = None,
    *,
    overwrite: bool = False,
    force: bool | None = None,
) -> Path:
    """
    Validate a rule and write it into :func:`install_dir`.

    :param data: A rule mapping, or the path of a JSON rule file.
    :param name: Optional target name (with or without a ``.json`` suffix).
        It is sanitized to a single filename; when omitted the source filename
        (path input) or ``site_key`` (mapping input) is used.
    :param overwrite: Replace an existing file with the same target name.
    :param force: Deprecated alias for ``overwrite`` (kept for the CLI).
    :raises RuleError: when the input cannot be read/parsed, is structurally
        invalid, or the target exists and ``overwrite`` is false.
    :return: The installed file's path.
    """
    if isinstance(data, Mapping):
        payload = dict(data)
        rule = parse_rule(payload, source=name or "<memory>")
        text = _serialize_rule(payload)
        default_name = rule.site_key
    elif isinstance(data, (str, Path)):
        path = Path(data)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as e:
            raise RuleError(
                t("cannot read {path}: {err}").format(path=path, err=e)
            ) from e
        payload = _load_json_text(text, path.name)
        rule = parse_rule(payload, source=path.name)
        default_name = path.stem
    else:
        raise RuleError("install_rule expects a rule mapping or a file path")

    if force:
        overwrite = True

    chosen = name if name is not None and str(name).strip() else default_name
    filename = _safe_rule_filename(str(chosen), fallback=rule.site_key)

    target_dir = install_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuleError(f"cannot create {target_dir}: {e}") from e

    target = target_dir / filename
    if (target.exists() or target.is_symlink()) and not overwrite:
        raise RuleError(
            t("{target} already exists (pass --force to overwrite)").format(
                target=target
            )
        )

    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=".rule-", suffix=".json.tmp", dir=target_dir
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            os.replace(temp_name, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
            raise
    except OSError as e:
        raise RuleError(f"cannot write {target}: {e}") from e

    reload_rules()
    return target


def remove_rule(site_key: str) -> Path:
    """
    Delete the user-installed rule file backing ``site_key``.

    :raises RuleError: when the key is unknown, built-in, or its file is gone.
    """
    load_rules()
    key = _normalize_key(site_key)
    meta = _meta.get(key)
    if meta is None:
        raise RuleError(t("unknown rule source {site_key!r}").format(site_key=site_key))
    if meta.get("origin") == "builtin":
        raise RuleError(
            t("'{key}' is a built-in source and cannot be removed").format(key=key)
        )

    target = Path(meta.get("file", ""))
    if not target.is_file():
        raise RuleError(t("rule file not found: {target}").format(target=target))
    target.unlink()
    reload_rules()
    return target


# --------------------------------------------------------------------------- #
# refreshing installed user rules
# --------------------------------------------------------------------------- #
def _builtin_keys() -> set[str]:
    """Collect the ``site_key`` of every shipped builtin rule (best effort)."""
    keys: set[str] = set()
    try:
        builtin = files(_BUILTIN_PKG)
        for entry in builtin.iterdir():
            if not entry.name.endswith(".json") or _is_ignored(entry.name):
                continue
            try:
                data = _load_json_text(
                    entry.read_text(encoding="utf-8-sig"), entry.name
                )
                keys.add(_normalize_key(parse_rule(data, source=entry.name).site_key))
            except (OSError, ValueError):
                # A broken builtin must not make refreshing user rules fail.
                continue
    except Exception as e:  # pragma: no cover - packaging edge
        logger.warning("Could not inspect builtin rules: %s", e)
    return keys


def _selected(wanted: set[str] | None, *candidates: str) -> bool:
    """Whether any candidate matches the optional ``names`` filter."""
    if wanted is None:
        return True
    for candidate in candidates:
        text = str(candidate).strip()
        if text and (text.lower() in wanted or _normalize_key(text) in wanted):
            return True
    return False


def refresh_rules(names: Sequence[str] | None = None) -> RefreshResult:
    """
    Re-scan and reload the user rule files (offline).

    Every user rule is parsed and classified against the shipped builtin rules:

    * ``added``   -- a user rule whose ``site_key`` has no builtin counterpart;
    * ``updated`` -- a user rule that overrides a builtin with the same
      ``site_key``;
    * ``failed``  -- user files that could not be read/parsed, keyed by file
      stem (or by the requested name when it does not exist).

    No network access is performed.  Valid rules are registered immediately so
    a changed rule takes effect without restarting the process.

    :param names: Optional ``site_key`` / filename / file-stem filter.
    :return: A :class:`RefreshResult` with ``added``, ``updated`` and ``failed``.
    """
    requested: list[str] | None
    if names is None:
        requested = None
    elif isinstance(names, str):
        requested = [names]
    else:
        requested = [str(name) for name in names]

    wanted: set[str] | None = None
    if requested is not None:
        wanted = {name.strip().lower() for name in requested if name.strip()}

    reload_rules()
    register_all()

    builtin_keys = _builtin_keys()
    added: list[str] = []
    updated: list[str] = []
    failed: dict[str, str] = {}
    seen: set[str] = set()
    matched: set[str] = set()

    for directory in _user_dirs():
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            if _is_ignored(path.name):
                continue

            stem = path.stem
            matched.update({path.name.lower(), stem.lower()})

            try:
                text = path.read_text(encoding="utf-8-sig")
            except OSError as e:
                if _selected(wanted, path.name, stem):
                    failed[stem] = t("cannot read {path}: {err}").format(
                        path=path, err=e
                    )
                continue

            try:
                data = _load_json_text(text, path.name)
                rule = parse_rule(data, source=path.name)
            except ValueError as e:
                if _selected(wanted, path.name, stem):
                    failed[stem] = str(e)
                continue

            key = _normalize_key(rule.site_key)
            matched.add(key)
            if not _selected(wanted, path.name, stem, key):
                continue
            if key in seen:
                continue
            seen.add(key)

            if key in builtin_keys:
                updated.append(key)
            else:
                added.append(key)

    if requested is not None:
        for name in requested:
            key = _normalize_key(name)
            if name.strip().lower() in matched or key in matched:
                continue
            if key in builtin_keys:
                # A builtin-only name is valid, there is simply nothing to do.
                continue
            failed[name] = t("unknown rule source {site_key!r}").format(site_key=name)

    logger.debug(
        "Refreshed rules: %d added, %d updated, %d failed",
        len(added),
        len(updated),
        len(failed),
    )
    return {"added": added, "updated": updated, "failed": failed}


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
