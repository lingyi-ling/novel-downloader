#!/usr/bin/env python3
"""Read-only consistency audit for ``docs/supported-sites``.

The site documentation is generated from ``scripts/data/supported_sites.toml``,
but the real registry is the plugin tree:

* ``src/novel_downloader/plugins/sites/<key>/`` -- active sites
* ``src/novel_downloader/plugins/archived/<key>/`` -- archived sites

This script reads both sides (plus ``apps/constants.py`` and ``mkdocs.yml``)
and reports every drift without writing anything:

* a registry site with no catalogue entry,
* a catalogue entry with no plugin directory,
* a registry site with no detail page,
* a detail page for a key that is not in the registry,
* a detail page whose title / identifier / homepage / tag group differs from
  the catalogue,
* an index row that is missing, duplicated, broken or disagrees with the
  detail page,
* stale statistics in the index,
* unsupported navigation in ``mkdocs.yml``,
* a catalogue homepage whose domain contradicts the adapter's base URL.

Usage::

    python scripts/check_site_docs.py
    python scripts/check_site_docs.py --root .

Exit status is 0 when the documentation is consistent and 1 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DOC_DIR = Path("docs") / "supported-sites"
DETAIL_DIR = DOC_DIR / "site-details"
CATALOGUE_PATH = Path("scripts") / "data" / "supported_sites.toml"
LANGUAGE_MAP_PATH = Path("scripts") / "data" / "language_map.json"
CONSTANTS_PATH = Path("src") / "novel_downloader" / "apps" / "constants.py"
PLUGINS_DIR = Path("src") / "novel_downloader" / "plugins"
MKDOCS_PATH = Path("mkdocs.yml")

URL_CONSTANTS = ("HOMEPAGE_URL", "BASE_URL", "DEFAULT_BASE_URL")

_ROW_RE = re.compile(
    r"^\|\s*\[([^\]]+)\]\(\./site-details/([^)\s]+)\.md\)\s*"
    r"\|\s*`([^`]+)`\s*\|",
    re.MULTILINE,
)
_TITLE_RE = re.compile(r"^title:\s*(.+?)\s*$", re.MULTILINE)
_IDENTIFIER_RE = re.compile(r"^\*\s*标识符:\s*`([^`]+)`", re.MULTILINE)
_HOMEPAGE_RE = re.compile(r"^\*\s*主页:\s*(\S+)\s*$", re.MULTILINE)
_FRONT_MATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_FRONT_TAG_RE = re.compile(r"^\s*-\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class CatalogueEntry:
    """One ``[sites.<key>]`` block from ``supported_sites.toml``."""

    key: str
    name: str
    homepage: str
    languages: tuple[str, ...]
    tags: tuple[str, ...]
    status: str
    alt_domains: tuple[str, ...]
    url_examples: tuple[str, ...]


@dataclass(frozen=True)
class DetailPage:
    """The facts the audit needs from one detail Markdown file."""

    path: Path
    title: str
    identifier: str
    homepage: str
    tags: tuple[str, ...]


def _site_dirs(path: Path) -> set[str]:
    if not path.is_dir():
        return set()
    return {
        child.name
        for child in path.iterdir()
        if child.is_dir() and not child.name.startswith("__")
    }


def load_registry(root: Path = Path(".")) -> tuple[set[str], set[str]]:
    """Return ``(active_keys, archived_keys)`` from the plugin package tree."""
    plugins = root / PLUGINS_DIR
    return _site_dirs(plugins / "sites"), _site_dirs(plugins / "archived")


def load_catalogue(root: Path = Path(".")) -> dict[str, CatalogueEntry]:
    """Read the documentation catalogue (key -> display metadata)."""
    raw = tomllib.loads((root / CATALOGUE_PATH).read_text(encoding="utf-8"))
    entries: dict[str, CatalogueEntry] = {}
    for key, site in raw.get("sites", {}).items():
        entries[key] = CatalogueEntry(
            key=key,
            name=str(site["name"]),
            homepage=str(site["homepage"]),
            languages=tuple(str(lang) for lang in site.get("languages", [])),
            tags=tuple(str(tag) for tag in site.get("tags", [])),
            status=str(site.get("site_status", "")),
            alt_domains=tuple(str(url) for url in site.get("alt_domains", [])),
            url_examples=tuple(
                str(example["url"])
                for example in site.get("url_examples", [])
                if not example.get("hidden", False)
            ),
        )
    return entries


def load_language_map(root: Path = Path(".")) -> dict[str, str]:
    raw = json.loads((root / LANGUAGE_MAP_PATH).read_text(encoding="utf-8"))
    return {str(key).lower(): str(value) for key, value in raw.items()}


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


def load_constants(root: Path = Path(".")) -> tuple[dict[str, str], dict[str, str]]:
    """Read the download/search maps without importing the application."""
    path = root / CONSTANTS_PATH
    tree = ast.parse(path.read_text(encoding="utf-8"))
    wanted = ("DOWNLOAD_SUPPORT_SITES", "SEARCH_SUPPORT_SITES")
    found: dict[str, dict[str, str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in wanted:
                found[target.id] = _literal_str_dict(node.value, source=target.id)
    missing = [name for name in wanted if name not in found]
    if missing:
        raise ValueError(f"{path}: missing {', '.join(missing)}")
    return found["DOWNLOAD_SUPPORT_SITES"], found["SEARCH_SUPPORT_SITES"]


def load_code_urls(root: Path = Path(".")) -> dict[str, tuple[str, ...]]:
    """Return page URLs declared by each site's Python adapters."""
    plugins = root / PLUGINS_DIR / "sites"
    urls: dict[str, list[str]] = {}
    for site_dir in sorted(plugins.iterdir()):
        if not site_dir.is_dir() or site_dir.name.startswith("__"):
            continue
        values: list[str] = []
        for module in sorted(site_dir.glob("*.py")):
            try:
                tree = ast.parse(module.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                for stmt in node.body:
                    if not isinstance(stmt, ast.Assign):
                        continue
                    names = [
                        target.id
                        for target in stmt.targets
                        if isinstance(target, ast.Name)
                    ]
                    if any(
                        name in URL_CONSTANTS or name.endswith("_URL") for name in names
                    ):
                        try:
                            value = ast.literal_eval(stmt.value)
                        except (SyntaxError, ValueError):
                            continue
                        if isinstance(value, str):
                            values.append(value)
                    elif "BASE_URL_MAP" in names and isinstance(stmt.value, ast.Dict):
                        for map_value in stmt.value.values:
                            try:
                                value = ast.literal_eval(map_value)
                            except (SyntaxError, ValueError):
                                continue
                            if isinstance(value, str):
                                values.append(value)
        if values:
            urls[site_dir.name] = tuple(dict.fromkeys(values))
    return {key: tuple(value) for key, value in urls.items()}


def _expected_tags(
    entry: CatalogueEntry, language_map: dict[str, str]
) -> tuple[str, ...]:
    translated = [
        language_map.get(lang.strip().lower(), lang) for lang in entry.languages
    ]
    return tuple(dict.fromkeys([*translated, *entry.tags]))


def parse_detail(path: Path) -> DetailPage:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    front = _FRONT_MATTER_RE.search(text)
    front_text = front.group(1) if front else ""
    title = _TITLE_RE.search(front_text)
    identifier = _IDENTIFIER_RE.search(text)
    homepage = _HOMEPAGE_RE.search(text)
    tags = tuple(_FRONT_TAG_RE.findall(front_text))
    return DetailPage(
        path=path,
        title=title.group(1).strip() if title else "",
        identifier=identifier.group(1).strip() if identifier else "",
        homepage=homepage.group(1).strip() if homepage else "",
        tags=tags,
    )


def _normalise_url(url: str) -> str:
    return url.strip().rstrip("/")


def _normalise_domain(url: str) -> str:
    value = url.strip()
    if "://" not in value and "." in value:
        value = f"https://{value}"
    return urlparse(value).netloc.lower().removeprefix("www.")


def _domains_related(left: str, right: str) -> bool:
    """True when two non-empty hosts are equal or one is a subdomain."""
    if not left or not right:
        return False
    return left == right or left.endswith(f".{right}") or right.endswith(f".{left}")


def _stat_value(index_text: str, label: str) -> int | None:
    match = re.search(
        rf"^\|\s*[^|]*{re.escape(label)}[^|]*\|\s*(\d+)\s*\|",
        index_text,
        re.MULTILINE,
    )
    return int(match.group(1)) if match else None


def _check_mkdocs(root: Path, errors: list[str]) -> None:
    path = root / MKDOCS_PATH
    if not path.is_file():
        errors.append(f"mkdocs: {MKDOCS_PATH} is missing")
        return

    text = path.read_text(encoding="utf-8")
    for page in ("supported-sites/index.md", "supported-sites/tags.md"):
        if page not in text:
            errors.append(f"mkdocs: navigation does not include {page}")

    not_in_nav = re.search(r"not_in_nav:\s*\|?\s*\n((?:[ \t]+.*\n)+)", text)
    if not not_in_nav or "supported-sites/site-details/*" not in not_in_nav.group(1):
        errors.append(
            "mkdocs: 'supported-sites/site-details/*' is not under not_in_nav"
        )


def audit(root: Path = Path(".")) -> list[str]:
    """Return every consistency finding; an empty list means the docs are sound."""
    errors: list[str] = []
    active, archived = load_registry(root)
    catalogue = load_catalogue(root)
    languages = load_language_map(root)
    download, search = load_constants(root)
    code_urls = load_code_urls(root)

    # 1. Registry <-> catalogue coverage.
    for key in sorted(active - set(catalogue)):
        errors.append(f"registry: active site '{key}' has no catalogue entry")
    for key in sorted(archived - set(catalogue)):
        errors.append(f"registry: archived site '{key}' has no catalogue entry")
    for key in sorted(set(catalogue) - active - archived):
        errors.append(f"catalogue: entry '{key}' has no plugin directory")
    for key in sorted(active - set(download)):
        errors.append(f"registry: active site '{key}' is not in DOWNLOAD_SUPPORT_SITES")
    for key in sorted(catalogue):
        entry = catalogue[key]
        if key in active and entry.status == "archived":
            errors.append(f"catalogue: '{key}' is archived but its plugin is in sites/")
        if key in archived and entry.status != "archived":
            errors.append(
                f"catalogue: '{key}' is in archived/ but has status '{entry.status}'"
            )
        if entry.status not in {"active", "unstable", "archived"}:
            errors.append(f"catalogue: '{key}' has invalid status '{entry.status}'")

    # 2. One detail page per registry site, no strays.
    details = {path.stem: path for path in sorted((root / DETAIL_DIR).glob("*.md"))}
    expected = active | archived
    for key in sorted(expected - set(details)):
        errors.append(f"docs: missing detail page site-details/{key}.md")
    for key in sorted(set(details) - expected):
        errors.append(f"docs: stray detail page site-details/{key}.md")

    # 3. Detail page contents agree with the catalogue.
    for key in sorted(set(details) & set(catalogue)):
        page = parse_detail(details[key])
        entry = catalogue[key]
        prefix = f"docs: site-details/{key}.md:"
        if page.title != entry.name:
            errors.append(
                f"{prefix} title '{page.title}' != catalogue name '{entry.name}'"
            )
        if page.identifier != key:
            errors.append(f"{prefix} identifier '{page.identifier}' != '{key}'")
        if _normalise_url(page.homepage) != _normalise_url(entry.homepage):
            errors.append(
                f"{prefix} homepage '{page.homepage}' != catalogue '{entry.homepage}'"
            )
        expected_tags = _expected_tags(entry, languages)
        if set(page.tags) != set(expected_tags):
            errors.append(
                f"{prefix} tags {list(page.tags)} != catalogue tags "
                f"{list(expected_tags)}"
            )

    # 4. The index table mirrors the detail pages exactly.
    index_path = root / DOC_DIR / "index.md"
    index_text = index_path.read_text(encoding="utf-8")
    rows: dict[str, tuple[str, str]] = {}
    for name, linked_key, identifier in _ROW_RE.findall(index_text):
        if linked_key != identifier:
            errors.append(
                f"docs: index row '{identifier}' links to site-details/{linked_key}.md"
            )
        if identifier in rows:
            errors.append(f"docs: index lists '{identifier}' more than once")
        rows[identifier] = (name, linked_key)
        if not (root / DETAIL_DIR / f"{linked_key}.md").is_file():
            errors.append(f"docs: index links to missing site-details/{linked_key}.md")
    for key in sorted(expected - set(rows)):
        errors.append(f"docs: index has no row for '{key}'")
    for key in sorted(set(rows) - expected):
        errors.append(f"docs: index has a row for unknown site '{key}'")
    for key in sorted(set(rows) & set(catalogue)):
        name, _ = rows[key]
        if name != catalogue[key].name:
            errors.append(
                f"docs: index name for '{key}' is '{name}' != '{catalogue[key].name}'"
            )
    for key in sorted(set(details) - set(rows)):
        errors.append(f"docs: site-details/{key}.md is not linked from the index")

    # 5. Statistics in the index.
    stats = {
        "站点条目总数": len(catalogue),
        "支持下载": len(download),
        "支持搜索": len(search),
        "仅支持下载": len(set(download) - set(search)),
        "已归档": sum(1 for entry in catalogue.values() if entry.status == "archived"),
    }
    for label, wanted in stats.items():
        actual = _stat_value(index_text, label)
        if actual != wanted:
            errors.append(
                f"docs: index statistic '{label}' is {actual}, expected {wanted}"
            )

    # 6. Navigation.
    _check_mkdocs(root, errors)

    # 7. Catalogue URLs vs adapter URLs (domain level only, so paths and
    # locale subdomains stay free).
    for key in sorted(active & set(code_urls)):
        entry = catalogue[key]
        code_domains = {
            domain for url in code_urls[key] if (domain := _normalise_domain(url))
        }
        if not code_domains:
            continue
        catalogue_domains = {
            domain
            for url in (entry.homepage, *entry.alt_domains)
            if (domain := _normalise_domain(url))
        }
        if not any(
            _domains_related(domain, allowed)
            for domain in code_domains
            for allowed in catalogue_domains
        ):
            errors.append(
                f"catalogue: '{key}' homepage '{entry.homepage}' is unrelated "
                f"to adapter URL(s) {sorted(code_urls[key])}"
            )
        allowed_domains = catalogue_domains | code_domains
        for example in entry.url_examples:
            domain = _normalise_domain(example)
            if domain and not any(
                _domains_related(domain, allowed) for allowed in allowed_domains
            ):
                errors.append(
                    f"catalogue: '{key}' URL example '{example}' uses an "
                    "unexpected domain"
                )

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="repository root (default: current directory)",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()

    errors = audit(root)
    if errors:
        for error in errors:
            print(f"[error] {error}")
        print(f"[fail] {len(errors)} finding(s)")
        return 1

    active, archived = load_registry(root)
    catalogue = load_catalogue(root)
    print(
        f"[ok] {len(active)} active sites, {len(archived)} archived, "
        f"{len(catalogue)} catalogue entries and {len(catalogue)} detail pages "
        "are consistent"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
