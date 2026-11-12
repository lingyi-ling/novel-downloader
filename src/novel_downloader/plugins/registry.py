#!/usr/bin/env python3
"""
novel_downloader.plugins.registry
---------------------------------
"""

from __future__ import annotations

import contextlib
import importlib.util
import logging
import threading
from importlib import import_module
from typing import TYPE_CHECKING, Any, TypeVar

from novel_downloader.infra.i18n import t

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from novel_downloader.plugins.base.searcher import BaseSearcher
    from novel_downloader.plugins.protocols import (
        ClientProtocol,
        FetcherProtocol,
        ParserProtocol,
        ProcessorProtocol,
    )
    from novel_downloader.schemas import (
        ClientConfig,
        FetcherConfig,
        ParserConfig,
    )

    ClientBuilder = Callable[[str, ClientConfig | None], ClientProtocol]
    FetcherBuilder = Callable[[FetcherConfig], FetcherProtocol]
    ParserBuilder = Callable[[ParserConfig], ParserProtocol]
    ProcessorBuilder = Callable[[dict[str, Any]], ProcessorProtocol]

    C = TypeVar("C", bound=ClientProtocol)
    F = TypeVar("F", bound=FetcherProtocol)
    P = TypeVar("P", bound=ParserProtocol)
    R = TypeVar("R", bound=ProcessorProtocol)  # Processor
    S = TypeVar("S", bound=type[BaseSearcher])

_PLUGINS_PKG = "novel_downloader.plugins"
_RULE_MODULE = "novel_downloader.plugins.rule"

#: Registry table backing each plugin kind.
_KIND_ATTRS: dict[str, str] = {
    "client": "_client",
    "fetcher": "_fetchers",
    "parser": "_parsers",
    "processor": "_processors",
    "searcher": "_searchers",
}

#: Characters that would turn a site key into a module/filesystem path.
_FORBIDDEN_SITE_KEY_CHARS = frozenset("/\\.")


class PluginRegistry:
    def __init__(self) -> None:
        self._client: dict[str, ClientBuilder] = {}
        self._fetchers: dict[str, FetcherBuilder] = {}
        self._parsers: dict[str, ParserBuilder] = {}
        self._processors: dict[str, ProcessorBuilder] = {}
        self._searchers: dict[str, type[BaseSearcher]] = {}
        self._sources: list[str] = [_PLUGINS_PKG]
        #: Module name -> "ExcType: message" for plugins that failed to import.
        #: Caching them prevents a warning storm on every lookup and keeps a
        #: half-broken plugin from being re-executed over and over.
        self._import_failures: dict[str, str] = {}
        self._lock = threading.RLock()

    def register_fetcher(
        self, site_key: str | None = None
    ) -> Callable[[type[F]], type[F]]:
        def deco(cls: type[F]) -> type[F]:
            self._store("fetcher", self._registration_key(site_key, cls), cls)
            return cls

        return deco

    def register_parser(
        self, site_key: str | None = None
    ) -> Callable[[type[P]], type[P]]:
        def deco(cls: type[P]) -> type[P]:
            self._store("parser", self._registration_key(site_key, cls), cls)
            return cls

        return deco

    def register_client(
        self, site_key: str | None = None
    ) -> Callable[[type[C]], type[C]]:
        def deco(cls: type[C]) -> type[C]:
            self._store("client", self._registration_key(site_key, cls), cls)
            return cls

        return deco

    def register_processor(
        self, name: str | None = None
    ) -> Callable[[type[R]], type[R]]:
        def deco(cls: type[R]) -> type[R]:
            key = (name or self._derive_processor_key(cls.__module__)).lower()
            self._store("processor", key, cls)
            return cls

        return deco

    def register_searcher(self, site_key: str | None = None) -> Callable[[S], S]:
        def deco(cls: S) -> S:
            self._store("searcher", self._registration_key(site_key, cls), cls)
            return cls

        return deco

    def register_site(
        self,
        site_key: str,
        *,
        fetcher: FetcherBuilder | None = None,
        parser: ParserBuilder | None = None,
        client: ClientBuilder | None = None,
        searcher: type[BaseSearcher] | None = None,
    ) -> None:
        """
        Register a site's plugins directly, bypassing module-path discovery.

        Used by dynamic sources (e.g. the rule-driven "book source" plugins)
        whose site key is not tied to an importable module name.
        """
        key = self._validate_site_key(site_key)
        # ``register_site`` is used by dynamic sources (rules) that deliberately
        # replace their own previous registration.  Source precedence is
        # enforced by those callers, not by module-path ranking.
        with self._lock:
            if fetcher is not None:
                self._store("fetcher", key, fetcher, respect_precedence=False)
            if parser is not None:
                self._store("parser", key, parser, respect_precedence=False)
            if client is not None:
                self._store("client", key, client, respect_precedence=False)
            if searcher is not None:
                self._store("searcher", key, searcher, respect_precedence=False)

    def get_registered(self, site_key: str, kind: str) -> Any | None:
        """
        Return the class/builder registered for ``site_key``/``kind``.

        Unlike :meth:`get_fetcher` and friends this never imports a plugin and
        never falls back to a default, so it can be used to detect collisions
        before registering a dynamic (rule-based) source.
        """
        attr = _KIND_ATTRS.get(kind)
        if attr is None:
            raise ValueError(f"Unknown plugin kind: {kind!r}")
        key = self._validate_site_key(site_key)
        with self._lock:
            return getattr(self, attr).get(key)

    def has_plugin(self, site_key: str, kind: str) -> bool:
        """
        Whether ``kind`` for ``site_key`` is registered or importable.

        A module only has to exist to count: checking with
        :mod:`importlib.util` avoids executing a plugin (and its optional
        dependencies) just to answer the question.
        """
        attr = _KIND_ATTRS.get(kind)
        if attr is None:
            raise ValueError(f"Unknown plugin kind: {kind!r}")
        key = self._validate_site_key(site_key)
        with self._lock:
            if getattr(self, attr).get(key) is not None:
                return True
            sources = tuple(self._sources)

        suffix = f".processors.{key}" if kind == "processor" else f".sites.{key}.{kind}"
        for base in sources:
            try:
                if importlib.util.find_spec(f"{base}{suffix}") is not None:
                    return True
            except (AttributeError, ImportError, TypeError, ValueError):
                # A broken parent package is not an importable plugin.
                continue
        return False

    @property
    def import_failures(self) -> dict[str, str]:
        """Plugins that failed to import, keyed by dotted module name."""
        with self._lock:
            return dict(self._import_failures)

    def clear_import_failures(self) -> None:
        """Forget cached import failures so the next lookup retries them."""
        with self._lock:
            self._import_failures.clear()

    def _ensure_rule_site(self, site_key: str) -> None:
        """Best-effort registration of a rule-based source."""
        try:
            from novel_downloader.plugins.rule import ensure_registered
        except Exception:  # pragma: no cover - rule package always present
            return
        try:
            ensure_registered(site_key)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Failed to register rule source %r: %s", site_key, e)

    def get_fetcher(self, site: str, config: FetcherConfig) -> FetcherProtocol:
        key = self._validate_site_key(site)
        cls = self._fetchers.get(key)
        if cls is None:
            self._try_import_site(key, "fetcher")
            cls = self._fetchers.get(key)
        if cls is None:
            self._ensure_rule_site(key)
            cls = self._fetchers.get(key)

        if cls is None:
            raise ValueError(t("Unsupported site: {site!r}").format(site=site))
        return cls(config)

    def get_parser(self, site: str, config: ParserConfig) -> ParserProtocol:
        key = self._validate_site_key(site)
        cls = self._parsers.get(key)
        if cls is None:
            self._try_import_site(key, "parser")
            cls = self._parsers.get(key)
        if cls is None:
            self._ensure_rule_site(key)
            cls = self._parsers.get(key)

        if cls is None:
            raise ValueError(t("Unsupported site: {site!r}").format(site=site))
        return cls(config)

    def get_client(
        self,
        site: str,
        config: ClientConfig | None = None,
    ) -> ClientProtocol:
        key = self._validate_site_key(site)
        cls = self._client.get(key)
        if cls is None:
            self._try_import_site(key, "client")
            cls = self._client.get(key)
        if cls is None:
            self._ensure_rule_site(key)
            cls = self._client.get(key)

        if cls is None:
            from novel_downloader.plugins.common.client import CommonClient

            return CommonClient(key, config)
        return cls(key, config)

    def get_processor(self, name: str, config: dict[str, Any]) -> ProcessorProtocol:
        key = name.strip().lower()
        builder = self._processors.get(key)
        if builder is None:
            self._try_import_processor(key)
            builder = self._processors.get(key)
        if builder is None:
            raise ValueError(t("Unsupported processor: {name!r}").format(name=name))
        return builder(config)

    def get_searcher_class(self, site: str) -> type[BaseSearcher]:
        key = self._validate_site_key(site)
        cls = self._searchers.get(key)
        if cls is None:
            self._try_import_site(key, "searcher")
            cls = self._searchers.get(key)
        if cls is None:
            self._ensure_rule_site(key)
            cls = self._searchers.get(key)
        if cls is None:
            raise ValueError(t("Unsupported site: {site!r}").format(site=site))
        return cls

    def get_searcher_classes(
        self,
        sites: Sequence[str] | None = None,
        *,
        load_all_if_none: bool = True,
    ) -> list[type[BaseSearcher]]:
        if sites:
            classes: list[type[BaseSearcher]] = []
            for site in sites:
                with contextlib.suppress(ValueError):
                    classes.append(self.get_searcher_class(site))
            return classes

        if load_all_if_none:
            self._load_all_sites("searcher")
            self._load_all_rule_sites()
        with self._lock:
            return list(self._searchers.values())

    def _load_all_rule_sites(self) -> None:
        """Register every rule-based source that declares a search section."""
        try:
            from novel_downloader.plugins.rule import register_all
        except Exception:  # pragma: no cover - rule package always present
            return
        try:
            register_all()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Failed to register rule sources: %s", e)

    @staticmethod
    def _normalize_key(site_key: str) -> str:
        """
        Normalize a site key to the expected module basename:
          * lowercase
          * if first char is a digit, prefix with 'n'

        This is the shared identity used by the search cache as well, so it
        stays deliberately lenient; :meth:`_validate_site_key` adds the
        boundary checks where a key would become an import path.
        """
        key = site_key.strip().lower()
        if not key:
            raise ValueError("Site key cannot be empty")
        if key[0].isdigit():
            return f"n{key}"
        return key

    @classmethod
    def _validate_site_key(cls, site_key: str) -> str:
        """
        Normalize a site key and reject anything that could escape the plugin
        namespace.

        A site key is interpolated into an import path (and may come from a
        CLI flag or a rule file), so ``.``, ``/`` and ``\\`` are refused rather
        than producing a surprising module lookup or a slow failure.
        """
        key = cls._normalize_key(site_key)
        if any(char in _FORBIDDEN_SITE_KEY_CHARS for char in key) or any(
            char.isspace() for char in key
        ):
            raise ValueError(f"Invalid site key: {site_key!r}")
        return key

    def _registration_key(self, site_key: str | None, cls: Any) -> str:
        """Key for a ``register_*`` decorator, validated like every lookup."""
        return self._validate_site_key(site_key or cls.__module__.split(".")[-2])

    @staticmethod
    def _derive_processor_key(modname: str) -> str:
        """
        Take module path after '.processors.' as key.
        Fallback to the last module segment if '.processors.' is not found.
        """
        if ".processors." in modname:
            return modname.split(".processors.", 1)[1]
        return modname.split(".")[-1]

    def _source_rank(self, module_name: str | None) -> int:
        """
        Precedence of the source that defines a plugin; lower wins.

        ``_sources`` is ordered by precedence (``enable_local_plugins`` inserts
        at the front with ``override``).  Modules that do not belong to a
        configured source, and rule plugins that are generated at runtime, rank
        last so they cannot shadow a real site package.
        """
        if not module_name:
            return len(self._sources)
        if module_name == _RULE_MODULE or module_name.startswith(_RULE_MODULE + "."):
            return len(self._sources)
        for index, base in enumerate(self._sources):
            if module_name == base or module_name.startswith(base + "."):
                return index
        return len(self._sources)

    def _store(
        self,
        kind: str,
        key: str,
        builder: Any,
        *,
        respect_precedence: bool = True,
    ) -> None:
        """
        Record one builder, applying source precedence for decorator
        registrations.

        Import order must not decide which implementation wins: without this
        check a user plugin could shadow a built-in during an aggregate search
        but lose to it during an explicit lookup, depending on what was
        imported first.
        """
        attr = _KIND_ATTRS[kind]
        with self._lock:
            table: dict[str, Any] = getattr(self, attr)
            previous = table.get(key)
            if previous is not None and previous is not builder and respect_precedence:
                previous_module = getattr(previous, "__module__", "")
                new_module = getattr(builder, "__module__", "")
                previous_rank = self._source_rank(previous_module)
                new_rank = self._source_rank(new_module)
                if previous_rank < new_rank:
                    logger.warning(
                        "Ignoring site plugin %r for %s from %s: %s already "
                        "provides it with higher precedence",
                        key,
                        kind,
                        new_module,
                        previous_module,
                    )
                    return
                if previous_rank == new_rank:
                    logger.warning(
                        "Duplicate site plugin key %r for %s: %s replaces %s",
                        key,
                        kind,
                        new_module,
                        previous_module,
                    )
            table[key] = builder

    @staticmethod
    def _iter_site_names(pkg: Any) -> list[str]:
        """Return the importable site subpackages of *pkg* (unsorted)."""
        import importlib.resources as res

        names: list[str] = []
        for entry in res.files(pkg).iterdir():
            name = entry.name
            if name.startswith("_") or not name.isidentifier():
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:  # pragma: no cover - unreadable directory
                continue
            if is_dir:
                names.append(name)
        return names

    def _record_import_failure(self, modname: str, exc: Exception) -> None:
        # Caller holds ``self._lock``.
        self._import_failures[modname] = f"{type(exc).__name__}: {exc}"

    def _rollback_partial(
        self, modname: str, snapshots: dict[str, dict[str, Any]]
    ) -> None:
        """
        Undo registrations made by a module that raised during import.

        Only entries whose builder is defined by the failing module (or one of
        its submodules) are removed, so a registration performed by an
        unrelated module imported earlier is left alone.
        """
        for attr, before in snapshots.items():
            table: dict[str, Any] = getattr(self, attr)
            for key, value in list(table.items()):
                if before.get(key) is value:
                    continue
                module = getattr(value, "__module__", "")
                if module == modname or module.startswith(modname + "."):
                    if key in before:
                        table[key] = before[key]
                    else:
                        table.pop(key, None)

    def _import_plugin_module(self, modname: str) -> tuple[str, Exception | None]:
        """
        Import one plugin module, isolating failures and caching them.

        :return: ``("ok", None)`` when imported, ``("missing", None)`` when the
            module itself does not exist, or ``("failed", exc)`` when it exists
            but raised.  A failed import is cached (and retried only after
            :meth:`clear_import_failures`), and partial registrations made
            before the exception are rolled back.
        """
        with self._lock:
            if modname in self._import_failures:
                return "failed", None

            snapshots = {
                attr: dict(getattr(self, attr)) for attr in _KIND_ATTRS.values()
            }
            try:
                import_module(modname)
            except ModuleNotFoundError as e:
                if e.name and modname.startswith(e.name):
                    # The module does not exist at all: a normal miss.
                    return "missing", None
                # The module exists but one of its imports is missing: an
                # unusable plugin, not a missing kind of plugin.
                self._record_import_failure(modname, e)
                self._rollback_partial(modname, snapshots)
                return "failed", e
            except Exception as e:  # noqa: BLE001 - one bad plugin is not fatal
                self._record_import_failure(modname, e)
                self._rollback_partial(modname, snapshots)
                return "failed", e
            return "ok", None

    def _try_import_site(self, site_key: str, kind: str) -> None:
        """
        Attempt to import plugins for a given site/kind in order:
          1. built-in: `plugins.sites.<site>.<kind>`
          2. user-level: `<base>.sites.<site>.<kind>`
        """
        for base in tuple(self._sources):
            modname = f"{base}.sites.{site_key}.{kind}"
            status, exc = self._import_plugin_module(modname)
            if status == "ok":
                return
            if status == "failed" and exc is not None:
                logger.warning("Skipping plugin %s: %s", modname, exc)

    def _try_import_processor(self, key: str) -> None:
        """
        Attempt to import plugins for a given site/kind in order:
          1. built-in: `plugins.processors.<key>`
          2. user-level: `<base>.processors.<key>`
        """
        for base in tuple(self._sources):
            modname = f"{base}.processors.{key}"
            status, exc = self._import_plugin_module(modname)
            if status == "ok":
                return
            if status == "failed" and exc is not None:
                logger.warning("Skipping plugin %s: %s", modname, exc)

    def _load_all_sites(self, kind: str) -> None:
        """
        Scan all known plugin and import every `{namespace}.sites.<site>.<kind>`
        without failing if a site has no module for that kind.

        Site names are sorted so discovery does not depend on filesystem
        iteration order, and a broken source package is skipped instead of
        aborting the whole scan.
        """
        for base in tuple(self._sources):
            try:
                pkg = import_module(f"{base}.sites")
            except Exception as e:  # noqa: BLE001 - one bad source is not fatal
                logger.warning("Skipping plugin source %s: %s", base, e)
                continue

            try:
                names = self._iter_site_names(pkg)
            except Exception as e:  # noqa: BLE001 - packaging edge
                logger.warning("Could not enumerate %s: %s", pkg.__name__, e)
                continue

            for name in sorted(names):
                modname = f"{pkg.__name__}.{name}.{kind}"
                status, exc = self._import_plugin_module(modname)
                if status == "failed" and exc is not None:
                    # Present but unimportable (missing dependency, syntax
                    # error, ...): skip this site, keep the others searchable.
                    logger.warning("Skipping plugin %s: %s", modname, exc)

    def enable_local_plugins(
        self,
        local_plugins_path: str | None = None,
        override: bool = False,
    ) -> None:
        """
        Enable user-provided plugins under the `novel_plugins` namespace.

        Behavior:
          * If `local_plugins_path` is provided.
            * Add its parent dir to `sys.path`
            * Add its basename as a search base (namespace) to `self._sources`
          * If not provided, default to:
            * parent = os.getcwd()
            * namespace = "novel_plugins"
        """
        import os
        import sys

        if local_plugins_path:
            base = os.path.abspath(local_plugins_path)
            parent = os.path.dirname(base)
            namespace = os.path.basename(base)
        else:
            parent = os.getcwd()
            namespace = "novel_plugins"

        with self._lock:
            if parent not in sys.path:
                sys.path.append(parent)

            if namespace not in self._sources:
                if override:
                    self._sources.insert(0, namespace)
                else:
                    self._sources.append(namespace)


registrar = PluginRegistry()
