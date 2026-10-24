#!/usr/bin/env python3
"""
Generate docs/supported-sites/*

The site catalogue is read from ``scripts/data/supported_sites.toml``.  The
download/search support and the site priority are cross-checked against the
source code:

* download / search support: ``apps/constants.py``
* priority: the ``priority`` class attribute of each site searcher

Usage:
    python scripts/gen_site_docs.py --overwrite
    python scripts/gen_site_docs.py --clean
    python scripts/gen_site_docs.py --check
"""

from __future__ import annotations

import argparse
import ast
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

# ================================
# Path constants
# ================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOC_DIR = PROJECT_ROOT / "docs" / "supported-sites"
CONFIG_PATH = PROJECT_ROOT / "scripts" / "data" / "supported_sites.toml"
LANGUAGE_MAP_PATH = PROJECT_ROOT / "scripts" / "data" / "language_map.json"
CONSTANTS_PATH = PROJECT_ROOT / "src" / "novel_downloader" / "apps" / "constants.py"
PLUGINS_DIR = PROJECT_ROOT / "src" / "novel_downloader" / "plugins"
LANGUAGE_MAP = json.loads(LANGUAGE_MAP_PATH.read_text(encoding="utf-8"))

DOWNLOAD_CONST = "DOWNLOAD_SUPPORT_SITES"
SEARCH_CONST = "SEARCH_SUPPORT_SITES"
CONST_NAMES = (DOWNLOAD_CONST, SEARCH_CONST)


# ================================
# Templates
# ================================

TAGS_TEMPLATE = """\
# 标签索引 (站点)

<!-- material/tags { scope: true } -->
"""

INDEX_TEMPLATE = """\
# Supported Sites / 支持站点

!!! info "说明"
    本页面列出了 NovelDownloader 当前支持的站点, 并提供各站点的功能支持情况概览。

    点击站点名称进入详情页, 可查看 **URL 示例、站点状态、功能支持级别** 等信息。

---

## 站点统计 (Statistics)

| 指标 | 数量 |
| ---- | ---- |
| 站点条目总数 (`supported_sites.toml`) | {total} |
| 支持下载 (`DOWNLOAD_SUPPORT_SITES`) | {download} |
| 支持搜索 (`SEARCH_SUPPORT_SITES`) | {search} |
| 仅支持下载 (不参与搜索) | {download_only} |
| 已归档 (archived) | {archived} |

!!! note "统计口径"
    * **支持下载 / 支持搜索**: 以 `src/novel_downloader/apps/constants.py`
      中注册的站点集合为准。
    * **优先级**: 搜索结果使用的站点优先级 (`BaseSearcher.priority`),
      **数值越小越靠前**; 未注册搜索器时显示 `—`。
    * 站点条目总数来自 `scripts/data/supported_sites.toml`, 包含已归档站点。

---

## 标签分类 (Tags)

参见：[标签索引](./tags.md)

---

## 支持站点总览 (Overview)

下表展示各站点的支持情况 (`下载` / `搜索` 表示是否已注册对应插件):

* :material-check: = 支持
* :material-close: = 不支持
* :material-help-circle: = 部分支持
* :material-open-in-new: = 站点原生支持 (需站内操作)

状态: :green_circle: Active / :orange_circle: Unstable / :red_circle: Archived

| 站点名称 | 标识符 | 状态 | 下载 | 搜索 | 优先级 | 分卷 | 图片 | 登录 |
| -------- | ------ | ---- | ---- | ---- | ------ | ---- | ---- | ---- |
{table}
"""

DETAIL_TEMPLATE = """\
---
title: {name}
tags:
{tags}
---

{description}

## 基本信息

* 标识符: `{id}`
* 主页: {homepage}{alt_domains}
* 语言: {languages}
* 站点状态: {status}
* 支持分卷: {volumes}
* 支持插图: {images}
* 支持登录: {login}
* 支持搜索: {search}

---

## URL 示例

{url_examples}

{notes}
"""


# ================================
# Mapping
# ================================

STATUS_MAP = {
    "active": (":green_circle:", "Active"),
    "unstable": (":orange_circle:", "Unstable"),
    "archived": (":red_circle:", "Archived"),
}

SUPPORT_MAP = {
    "yes": (":material-check:", "是"),
    "no": (":material-close:", "否"),
    "partial": (":material-help-circle:", "部分支持"),
    "external": (":material-open-in-new:", "站点支持，需站内操作"),
}

SUPPORT_KEYS = ("volumes", "images", "login", "search")


# ================================
# TypedDicts
# ================================


class SupportDict(TypedDict):
    volumes: Literal["yes", "no", "partial", "external"]
    images: Literal["yes", "no", "partial", "external"]
    login: Literal["yes", "no", "partial", "external"]
    search: Literal["yes", "no", "partial", "external"]


class LoginNotes(TypedDict):
    content: str


class UrlExample(TypedDict):
    title: str
    url: str
    hidden: bool
    book_id: NotRequired[str]
    chapter_id: NotRequired[str]


class NoteItem(TypedDict):
    title: str
    content: str


class SiteConfig(TypedDict):
    id: str
    name: str
    homepage: str
    alt_domains: list[str]
    site_status: Literal["active", "unstable", "archived"]
    description: str
    languages: list[str]
    tags: list[str]
    supports: SupportDict
    login_notes: LoginNotes
    url_examples: list[UrlExample]
    notes: list[NoteItem]


@dataclass(frozen=True)
class ClassInfo:
    bases: tuple[str, ...]
    priority: int | None
    site_name: str | None


# ================================
# Utility
# ================================


def lang_to_cn(code: str) -> str:
    normalized = code.strip().lower()
    return LANGUAGE_MAP.get(normalized, code)


def render_status(value: str, *, icon_only: bool = False) -> str:
    icon, txt = STATUS_MAP.get(value, (":material-help-circle:", value))
    return icon if icon_only else f"{icon} {txt}"


def render_support(value: str, *, icon_only: bool = False) -> str:
    icon, txt = SUPPORT_MAP.get(value, (":material-help-circle:", value))
    return icon if icon_only else f"{icon} {txt}"


def _literal_int(node: ast.expr) -> int | None:
    try:
        value = ast.literal_eval(node)
    except (SyntaxError, ValueError):
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _literal_str(node: ast.expr) -> str | None:
    try:
        value = ast.literal_eval(node)
    except (SyntaxError, ValueError):
        return None
    return value if isinstance(value, str) else None


def _literal_str_dict(node: ast.expr, *, source: str) -> dict[str, str]:
    if not isinstance(node, ast.Dict):
        raise ValueError(f"{source} is not a dict literal")

    result: dict[str, str] = {}
    for key_node, value_node in zip(node.keys, node.values, strict=True):
        if key_node is None:
            continue
        key = ast.literal_eval(key_node)
        value = ast.literal_eval(value_node)
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError(f"{source} contains a non-string entry")
        result[key] = value
    return result


def load_constants(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Read the download/search site maps without importing the package."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, dict[str, str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in CONST_NAMES:
                found[target.id] = _literal_str_dict(node.value, source=target.id)

    missing = [name for name in CONST_NAMES if name not in found]
    if missing:
        raise ValueError(f"{path}: missing {', '.join(missing)}")
    return found[DOWNLOAD_CONST], found[SEARCH_CONST]


def _parse_class(node: ast.ClassDef) -> ClassInfo:
    priority: int | None = None
    site_name: str | None = None
    for stmt in node.body:
        if not isinstance(stmt, ast.Assign):
            continue
        for target in stmt.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id == "priority":
                value = _literal_int(stmt.value)
                if value is not None:
                    priority = value
            elif target.id == "site_name":
                value = _literal_str(stmt.value)
                if value is not None:
                    site_name = value

    # ``ast.unparse`` may return a dotted name; only the class name is needed.
    bases = tuple(ast.unparse(base).rsplit(".", 1)[-1] for base in node.bases)
    return ClassInfo(bases=bases, priority=priority, site_name=site_name)


def load_searcher_priorities(plugins_dir: Path) -> dict[str, int]:
    """Resolve each site searcher's ``priority``, including inherited values."""
    definitions: dict[str, ClassInfo] = {}
    site_classes: dict[str, list[str]] = {}

    for path in sorted(plugins_dir.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        class_names: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                class_names.append(node.name)
                definitions.setdefault(node.name, _parse_class(node))
        if path.name == "searcher.py" and path.parent.parent.name in {
            "sites",
            "archived",
        }:
            site_classes[path.parent.name] = class_names

    def resolve(name: str, seen: frozenset[str]) -> int | None:
        if name in seen:
            return None
        info = definitions.get(name)
        if info is None:
            return None
        if info.priority is not None:
            return info.priority
        for base in info.bases:
            value = resolve(base, seen | {name})
            if value is not None:
                return value
        return None

    priorities: dict[str, int] = {}
    for site_id, class_names in site_classes.items():
        target = next(
            (
                name
                for name in class_names
                if definitions.get(name, ClassInfo((), None, None)).site_name == site_id
            ),
            class_names[0] if class_names else None,
        )
        if target is None:
            continue
        value = resolve(target, frozenset())
        if value is not None:
            priorities[site_id] = value
    return priorities


def load_searcher_site_ids(plugins_dir: Path) -> set[str]:
    """Return the site ids that ship a ``searcher.py`` module."""
    result = set()
    for sub in ("sites", "archived"):
        result.update(
            path.parent.name for path in (plugins_dir / sub).glob("*/searcher.py")
        )
    return result


def load_adapter_ids(plugins_dir: Path) -> tuple[set[str], set[str]]:
    """Return the site ids that ship a fetcher under ``sites/`` / ``archived/``."""
    active = {path.parent.name for path in (plugins_dir / "sites").glob("*/fetcher.py")}
    archived = {
        path.parent.name for path in (plugins_dir / "archived").glob("*/fetcher.py")
    }
    return active, archived


def load_site_configs(path: Path) -> list[SiteConfig]:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    result: list[SiteConfig] = []
    for sid, cfg in raw.get("sites", {}).items():
        cfg["id"] = sid
        result.append(cfg)  # type: ignore[arg-type]
    return result


def validate_sites(
    sites: list[SiteConfig],
    download_ids: set[str],
    search_ids: set[str],
    searcher_ids: set[str],
    priorities: dict[str, int],
) -> list[str]:
    """Cross-check the TOML catalogue against the source code facts."""
    errors: list[str] = []
    site_ids = {site["id"] for site in sites}

    if not site_ids:
        errors.append("no sites found in the catalogue")

    for name, ids in (
        (DOWNLOAD_CONST, download_ids),
        (SEARCH_CONST, search_ids),
    ):
        missing = sorted(ids - site_ids)
        if missing:
            errors.append(
                f"{name} contains ids missing from TOML: {', '.join(missing)}"
            )

    missing_searchers = sorted(search_ids - searcher_ids)
    if missing_searchers:
        errors.append(
            "search-supported sites without searcher.py: "
            + ", ".join(missing_searchers)
        )
    missing_priorities = sorted(search_ids - priorities.keys())
    if missing_priorities:
        errors.append(
            "search-supported sites without resolved priority: "
            + ", ".join(missing_priorities)
        )
    not_downloadable = sorted(search_ids - download_ids)
    if not_downloadable:
        errors.append(
            "search-supported sites missing from DOWNLOAD_SUPPORT_SITES: "
            + ", ".join(not_downloadable)
        )

    for site in sites:
        sid = site["id"]
        if site.get("site_status") not in STATUS_MAP:
            errors.append(f"{sid}: invalid site_status {site.get('site_status')!r}")
        supports = site.get("supports", {})
        for key in SUPPORT_KEYS:
            if supports.get(key) not in SUPPORT_MAP:
                errors.append(f"{sid}: invalid supports.{key} {supports.get(key)!r}")
        for ex in site.get("url_examples", []):
            if not ex.get("title") or not ex.get("url"):
                errors.append(f"{sid}: url example without title/url")

    return errors


def build_diff_report(
    sites: list[SiteConfig],
    download_ids: set[str],
    search_ids: set[str],
    searcher_ids: set[str],
    active_adapters: set[str],
    archived_adapters: set[str],
) -> list[str]:
    """Summarise TOML/constants/adapter differences without affecting the exit code."""
    site_ids = {site["id"] for site in sites}
    status = {site["id"]: site.get("site_status") for site in sites}
    registered = download_ids | search_ids

    lines = [
        "[audit] TOML vs constants",
        f"  constants: download={len(download_ids)} search={len(search_ids)}; "
        f"toml={len(sites)}; adapters: sites={len(active_adapters)} "
        f"archived={len(archived_adapters)}",
    ]

    def emit(title: str, ids: list[str], detail: str = "") -> None:
        if not ids:
            return
        suffix = f" ({detail})" if detail else ""
        lines.append(f"  {title}: {len(ids)}{suffix}")
        lines.extend(f"    - {sid}" for sid in ids)

    unregistered = sorted(site_ids - download_ids)
    if unregistered:
        archived = [sid for sid in unregistered if status.get(sid) == "archived"]
        active = [sid for sid in unregistered if status.get(sid) != "archived"]
        emit("TOML entries absent from DOWNLOAD_SUPPORT_SITES", unregistered)
        if active:
            emit("  of which active entries", active)
        if archived:
            emit("  of which archived entries", archived)

    orphan_adapters = sorted(active_adapters - download_ids)
    emit("active adapters (sites/) absent from DOWNLOAD_SUPPORT_SITES", orphan_adapters)

    orphan_searchers = sorted(active_adapters & searcher_ids - search_ids)
    emit("active searcher.py absent from SEARCH_SUPPORT_SITES", orphan_searchers)

    archived_registered = sorted(archived_adapters & registered)
    emit("archived adapters still registered in constants", archived_registered)

    contradictions: list[str] = []
    for site in sites:
        sid = site["id"]
        value = site["supports"]["search"]
        is_registered = sid in search_ids
        if value == "yes" and not is_registered:
            contradictions.append(f"{sid}: search=yes but not registered")
        elif value == "no" and is_registered:
            contradictions.append(f"{sid}: search=no but registered")
    lines.append(
        "  supports.search contradictions with SEARCH_SUPPORT_SITES: "
        f"{len(contradictions)}"
    )
    lines.extend(f"    - {item}" for item in contradictions)

    external_only = sorted(
        site["id"]
        for site in sites
        if site["supports"]["search"] == "external" and site["id"] not in search_ids
    )
    lines.append(
        "  site-native search only (search=external, not aggregated): "
        f"{len(external_only)}"
    )
    return lines


# ================================
# Builder
# ================================


class SupportedSiteBuilder:
    def __init__(
        self,
        download_ids: set[str],
        search_ids: set[str],
        priorities: dict[str, int],
    ):
        self.download_ids = download_ids
        self.search_ids = search_ids
        self.priorities = priorities
        self.sites: list[SiteConfig] = []

    def add_site(self, site: SiteConfig) -> None:
        self.sites.append(site)

    def render(self) -> dict[str, str]:
        """Render every generated file, keyed by its path relative to DOC_DIR."""
        sites = sorted(self.sites, key=lambda site: site["id"].lower())
        site_ids = {site["id"] for site in sites}

        download_count = len(self.download_ids)
        search_count = len(self.search_ids)

        # The overview columns must agree with the source constants.
        table_downloads = sum(1 for site in sites if site["id"] in self.download_ids)
        table_searches = sum(1 for site in sites if site["id"] in self.search_ids)
        if table_downloads != download_count:
            raise ValueError(
                f"table download count {table_downloads} != {download_count}"
            )
        if table_searches != search_count:
            raise ValueError(f"table search count {table_searches} != {search_count}")

        stats = {
            "total": len(sites),
            "download": download_count,
            "search": search_count,
            "download_only": len(self.download_ids - self.search_ids),
            "archived": sum(
                1 for site in sites if site.get("site_status") == "archived"
            ),
        }

        pages: dict[str, str] = {}
        rows = [self._make_overview_row(site) for site in sites]
        pages["index.md"] = INDEX_TEMPLATE.format(table="\n".join(rows), **stats)
        pages["tags.md"] = TAGS_TEMPLATE
        for site in sites:
            pages[f"site-details/{site['id']}.md"] = self._make_detail_page(site)

        # Defensive check: every generated site id must be accounted for.
        if not site_ids:
            raise ValueError("no site pages generated")
        return pages

    def build(self, target_dir: Path, overwrite: bool = False) -> None:
        """Write index.md, tags.md and detail pages into ``target_dir``."""
        target_dir.mkdir(parents=True, exist_ok=True)
        for rel, content in self.render().items():
            self._write_file(target_dir / rel, content, overwrite)

    def check(self, target_dir: Path) -> bool:
        """Return True when the on-disk documentation already matches."""
        rendered = self.render()
        missing: list[str] = []
        stale: list[str] = []

        for rel, content in rendered.items():
            path = target_dir / rel
            if not path.is_file():
                missing.append(rel)
            elif path.read_text(encoding="utf-8") != content:
                stale.append(rel)

        expected = set(rendered)
        extra: list[str] = []
        details_dir = target_dir / "site-details"
        if details_dir.is_dir():
            for path in sorted(details_dir.glob("*.md")):
                rel = f"site-details/{path.name}"
                if rel not in expected:
                    extra.append(rel)

        for rel in missing:
            print(f"[missing] {rel}")
        for rel in stale:
            print(f"[stale] {rel}")
        for rel in extra:
            print(f"[extra] {rel}")

        if missing or stale or extra:
            return False

        print(f"[ok] {len(rendered)} generated files are up to date")
        return True

    def _write_file(self, path: Path, content: str, overwrite: bool) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not overwrite:
            print(f"[skip] {path}")
            return
        path.write_text(content, encoding="utf-8", newline="\n")
        print(f"[write] {path}")

    def _make_overview_row(self, site: SiteConfig) -> str:
        sid = site["id"]
        s = site["supports"]
        download = render_support(
            "yes" if sid in self.download_ids else "no", icon_only=True
        )
        search = render_support(
            "yes" if sid in self.search_ids else "no", icon_only=True
        )
        priority = (
            str(self.priorities[sid])
            if sid in self.search_ids and sid in self.priorities
            else "—"
        )
        return (
            f"| [{site['name']}](./site-details/{sid}.md)"
            f" | `{sid}`"
            f" | {render_status(site['site_status'], icon_only=True)}"
            f" | {download}"
            f" | {search}"
            f" | {priority}"
            f" | {render_support(s['volumes'], icon_only=True)}"
            f" | {render_support(s['images'], icon_only=True)}"
            f" | {render_support(s['login'], icon_only=True)} |"
        )

    def _make_detail_page(self, site: SiteConfig) -> str:
        sid = site["id"]
        supports = site["supports"]

        if site.get("alt_domains"):
            alt_lines = ["\n* 备用域名:"]
            for domain in site["alt_domains"]:
                alt_lines.append(f"    * {domain}")
            alt_domains_block = "\n".join(alt_lines)
        else:
            alt_domains_block = ""

        url_blocks = []
        for ex in site.get("url_examples", []):
            if ex.get("hidden", False):
                continue

            blk = f"### {ex['title']}\n\nURL:\n\n```\n{ex['url']}\n```\n"
            if bid := ex.get("book_id"):
                blk += f"\n* Book ID: `{bid}`"
            if cid := ex.get("chapter_id"):
                blk += f"\n* Chapter ID: `{cid}`"

            url_blocks.append(blk)

        notes = []
        for note in site.get("notes", []):
            notes.append(f"## {note['title']}\n\n{note['content'].strip()}")

        # tags = languages + explicit tags (preserve order, drop duplicates)
        all_tags = list(
            dict.fromkeys(
                [lang_to_cn(lang) for lang in site.get("languages", [])]
                + site.get("tags", [])
            )
        )

        all_notes = "---\n\n" + "\n\n".join(notes) if notes else ""

        result = DETAIL_TEMPLATE.format(
            name=site["name"],
            id=sid,
            homepage=site["homepage"],
            alt_domains=alt_domains_block,
            description=site["description"].strip(),
            languages=", ".join(lang_to_cn(lang) for lang in site.get("languages", [])),
            status=render_status(site["site_status"]),
            volumes=render_support(supports["volumes"]),
            images=render_support(supports["images"]),
            login=render_support(supports["login"]),
            search=render_support(supports["search"]),
            tags="".join(f"  - {tag}\n" for tag in all_tags),
            url_examples="\n\n".join(url_blocks),
            notes=all_notes,
        )
        return result.rstrip() + "\n"


# ================================
# Main
# ================================


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero when generated files are missing or stale",
    )
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--constants", type=Path, default=CONSTANTS_PATH)
    parser.add_argument("--plugins", type=Path, default=PLUGINS_DIR)
    parser.add_argument("--target", type=Path, default=DOC_DIR)
    args = parser.parse_args()

    download, search = load_constants(args.constants)
    priorities = load_searcher_priorities(args.plugins)
    searcher_ids = load_searcher_site_ids(args.plugins)
    active_adapters, archived_adapters = load_adapter_ids(args.plugins)
    sites = load_site_configs(args.config)

    errors = validate_sites(sites, set(download), set(search), searcher_ids, priorities)
    if errors:
        for error in errors:
            print(f"[error] {error}")
        raise SystemExit(1)

    print(
        f"[constants] download={len(download)} search={len(search)} "
        f"toml={len(sites)} priorities={len(priorities)}"
    )
    for line in build_diff_report(
        sites,
        set(download),
        set(search),
        searcher_ids,
        active_adapters,
        archived_adapters,
    ):
        print(line)

    builder = SupportedSiteBuilder(set(download), set(search), priorities)
    for site in sites:
        builder.add_site(site)

    if args.check:
        raise SystemExit(0 if builder.check(args.target) else 1)

    if args.clean and args.target.exists():
        import shutil

        shutil.rmtree(args.target)
        print(f"[clean] removed {args.target}")

    builder.build(args.target, overwrite=args.overwrite)


if __name__ == "__main__":
    main()
