#!/usr/bin/env python3
"""Static guards for ``docs/supported-sites`` versus the plugin registry.

``scripts/check_site_docs.py`` owns the audit logic; these tests run it against
the real tree and against a synthetic one so the "new site without a doc" gate
is exercised even when the repository is currently clean.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = ROOT / "scripts" / "check_site_docs.py"
_spec = importlib.util.spec_from_file_location("check_site_docs", _SCRIPT)
assert _spec and _spec.loader
site_docs = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = site_docs
_spec.loader.exec_module(site_docs)

ACTIVE, ARCHIVED = site_docs.load_registry(ROOT)


def test_docs_match_registry_and_catalogue() -> None:
    findings = site_docs.audit(ROOT)
    assert findings == [], "\n".join(findings)


def test_registry_matches_support_constants() -> None:
    download, _ = site_docs.load_constants(ROOT)
    catalogue = site_docs.load_catalogue(ROOT)
    assert set(download) == ACTIVE
    assert set(catalogue) == ACTIVE | ARCHIVED
    assert all(catalogue[key].status == "archived" for key in ARCHIVED)
    assert all(catalogue[key].status != "archived" for key in ACTIVE)


def test_detail_page_exists_for_every_active_site() -> None:
    active, _ = site_docs.load_registry(ROOT)
    details = ROOT / "docs" / "supported-sites" / "site-details"
    missing = sorted(key for key in active if not (details / f"{key}.md").is_file())
    assert missing == []


@pytest.mark.parametrize("key", sorted(ACTIVE))
def test_active_site_detail_page_matches_catalogue(key: str) -> None:
    catalogue = site_docs.load_catalogue(ROOT)
    page = site_docs.parse_detail(
        ROOT / "docs" / "supported-sites" / "site-details" / f"{key}.md"
    )
    entry = catalogue[key]
    assert page.identifier == key
    assert page.title == entry.name
    assert page.homepage.rstrip("/") == entry.homepage.rstrip("/")


def _write_fixture(root: Path, *, with_detail: bool = True) -> None:
    """Create the smallest tree the audit accepts."""
    (root / "src/novel_downloader/plugins/sites/demo").mkdir(parents=True)
    (root / "src/novel_downloader/plugins/archived").mkdir(parents=True)
    (root / "src/novel_downloader/plugins/sites/demo/fetcher.py").write_text(
        'class DemoFetcher:\n    BASE_URL = "https://demo.example/"\n',
        encoding="utf-8",
    )
    (root / "src/novel_downloader/apps").mkdir(parents=True)
    (root / "src/novel_downloader/apps/constants.py").write_text(
        'DOWNLOAD_SUPPORT_SITES = {"demo": "演示 (demo)"}\n'
        'SEARCH_SUPPORT_SITES = {"demo": "演示"}\n',
        encoding="utf-8",
    )
    (root / "scripts/data").mkdir(parents=True)
    (root / "scripts/data/supported_sites.toml").write_text(
        "[sites.demo]\n"
        'name = "演示"\n'
        'homepage = "https://demo.example/"\n'
        "alt_domains = []\n"
        'site_status = "active"\n'
        'languages = ["zh-CN"]\n'
        'tags = ["转载站"]\n',
        encoding="utf-8",
    )
    (root / "scripts/data/language_map.json").write_text(
        '{"zh-cn": "简体中文"}', encoding="utf-8"
    )
    (root / "mkdocs.yml").write_text(
        "nav:\n"
        "  - 支持站点:\n"
        "    - 总览: supported-sites/index.md\n"
        "    - 标签: supported-sites/tags.md\n"
        "not_in_nav: |\n"
        "  supported-sites/site-details/*\n",
        encoding="utf-8",
    )
    docs = root / "docs/supported-sites"
    (docs / "site-details").mkdir(parents=True)
    (docs / "index.md").write_text(
        "| 指标 | 数量 |\n"
        "| ---- | ---- |\n"
        "| 站点条目总数 (`supported_sites.toml`) | 1 |\n"
        "| 支持下载 (`DOWNLOAD_SUPPORT_SITES`) | 1 |\n"
        "| 支持搜索 (`SEARCH_SUPPORT_SITES`) | 1 |\n"
        "| 仅支持下载 (不参与搜索) | 0 |\n"
        "| 已归档 (archived) | 0 |\n"
        "\n"
        "| 站点名称 | 标识符 |\n"
        "| -------- | ------ |\n"
        "| [演示](./site-details/demo.md) | `demo` |\n",
        encoding="utf-8",
    )
    if with_detail:
        (docs / "site-details/demo.md").write_text(
            "---\n"
            "title: 演示\n"
            "tags:\n"
            "  - 简体中文\n"
            "  - 转载站\n"
            "---\n"
            "\n"
            "* 标识符: `demo`\n"
            "* 主页: https://demo.example/\n",
            encoding="utf-8",
        )


def test_fixture_is_clean(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    assert site_docs.audit(tmp_path) == []


def test_new_registry_site_without_doc_is_reported(tmp_path: Path) -> None:
    _write_fixture(tmp_path, with_detail=False)
    findings = site_docs.audit(tmp_path)
    assert any("missing detail page site-details/demo.md" in item for item in findings)
    assert any(
        "index links to missing site-details/demo.md" in item for item in findings
    )


def test_catalogue_homepage_must_match_adapter_domain(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    fetcher = tmp_path / "src/novel_downloader/plugins/sites/demo/fetcher.py"
    fetcher.write_text(
        'class DemoFetcher:\n    BASE_URL = "https://other.example/"\n',
        encoding="utf-8",
    )
    findings = site_docs.audit(tmp_path)
    assert any("is unrelated to adapter URL" in item for item in findings)


def test_catalogue_url_example_must_use_a_known_domain(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    catalogue = tmp_path / "scripts/data/supported_sites.toml"
    catalogue.write_text(
        catalogue.read_text(encoding="utf-8")
        + "\n[[sites.demo.url_examples]]\n"
        + 'title = "Book URL"\n'
        + 'url = "https://stray.example/book/1"\n'
        + "hidden = false\n",
        encoding="utf-8",
    )
    findings = site_docs.audit(tmp_path)
    assert any("uses an unexpected domain" in item for item in findings)
