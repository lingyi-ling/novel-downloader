#!/usr/bin/env python3
"""
novel_downloader.infra.i18n
---------------------------

Runtime localization helpers.

Translations live in ``novel_downloader/locales/<code>/LC_MESSAGES/messages.mo``
and are compiled from the ``.po`` sources at build time (see
``scripts/compile_mo.py``).

English (``en_US``) is the **source language**: message ids are plain English
strings, so no catalog is shipped for it and any unknown id falls back to the
msgid itself.

The active language is resolved lazily on every ``t()`` call, so switching it
at runtime (``set_language``) takes effect immediately. Catalogs are cached per
language code.
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_LANGUAGE",
    "LANG_ENV_VAR",
    "LANGUAGE_ALIASES",
    "SOURCE_LANGUAGE",
    "SUPPORTED_LANGUAGES",
    "available_languages",
    "get_language",
    "get_translation",
    "normalize_language",
    "reload_catalogs",
    "set_language",
    "t",
]

import gettext
import logging
import os
from importlib.resources import files

from novel_downloader.infra.persistence.state import state_mgr

logger = logging.getLogger(__name__)

#: Language used when nothing else is configured.
DEFAULT_LANGUAGE = "zh_CN"

#: Language the message ids are written in.
SOURCE_LANGUAGE = "en_US"

#: Environment variable that overrides the persisted preference.
LANG_ENV_VAR = "NOVEL_LANG"

#: Registry of selectable languages: ``code -> native display name``.
#: Adding a language means dropping a ``messages.po`` there and adding a row.
SUPPORTED_LANGUAGES: dict[str, str] = {
    "zh_CN": "简体中文",
    "en_US": "English",
}

#: Accepted user input aliases, normalized to a registered language code.
LANGUAGE_ALIASES: dict[str, str] = {
    "zh": "zh_CN",
    "zh-cn": "zh_CN",
    "zh_cn": "zh_CN",
    "zh-hans": "zh_CN",
    "zh_hans": "zh_CN",
    "zh-sg": "zh_CN",
    "zh_sg": "zh_CN",
    "en": "en_US",
    "en-us": "en_US",
    "en_us": "en_US",
    "en-gb": "en_US",
    "en_gb": "en_US",
    "c": "en_US",
    "posix": "en_US",
}

_translations: dict[str, gettext.NullTranslations] = {}
_missing_catalogs_warned: set[str] = set()


def normalize_language(code: str | None) -> str:
    """
    Normalize a user-supplied language code to a registered code.

    Aliases such as ``zh``, ``zh-CN``, ``en`` or ``en_US`` are accepted; exact
    (case-insensitive) registry codes are accepted too.

    :param code: Raw language code, possibly an alias or empty.
    :return: A registered language code, or ``""`` if it cannot be resolved.
    """
    if not code:
        return ""

    raw = str(code).strip()
    if not raw:
        return ""

    underscored = raw.replace("-", "_")
    for candidate in (raw, underscored):
        alias = LANGUAGE_ALIASES.get(candidate.lower())
        if alias:
            return alias

    for registered in SUPPORTED_LANGUAGES:
        if registered.lower() == underscored.lower():
            return registered

    return ""


def available_languages() -> dict[str, str]:
    """
    Return the registered languages for menus/selectors.

    :return: A copy of the registry mapping ``code -> display name``.
    """
    return dict(SUPPORTED_LANGUAGES)


def get_language() -> str:
    """
    Resolve the active language code.

    Resolution order: ``NOVEL_LANG`` environment variable, then the persisted
    user preference, then :data:`DEFAULT_LANGUAGE`. Unknown values are skipped.

    :return: A registered language code.
    """
    env_code = normalize_language(os.environ.get(LANG_ENV_VAR))
    if env_code:
        return env_code

    saved_code = normalize_language(state_mgr.get_language())
    if saved_code:
        return saved_code

    return DEFAULT_LANGUAGE


def set_language(lang: str) -> str:
    """
    Validate and persist the interface language.

    :param lang: Language code or alias.
    :return: The normalized code on success, or ``""`` if unsupported
        (nothing is persisted in that case).
    """
    code = normalize_language(lang)
    if not code:
        return ""
    saved_code = state_mgr.get_language()
    if normalize_language(saved_code) != code:
        state_mgr.set_language(code)
    return code


def get_translation(lang: str | None = None) -> gettext.NullTranslations:
    """
    Return (and cache) the translation catalog for a language.

    Falls back to the source language (English) when the requested language is
    unknown, and to the msgid itself when its ``.mo`` file is missing.

    :param lang: Language code or alias; the active language when omitted.
    :return: A ``gettext`` translations object.
    """
    code = normalize_language(lang) if lang else get_language()
    if not code:
        code = SOURCE_LANGUAGE

    cached = _translations.get(code)
    if cached is not None:
        return cached

    translation = _load_catalog(code)
    _translations[code] = translation
    return translation


def _load_catalog(code: str) -> gettext.NullTranslations:
    """Load the ``.mo`` catalog for ``code``, warning once if it is missing."""
    if code == SOURCE_LANGUAGE:
        return gettext.NullTranslations()

    try:
        mo_path = files("novel_downloader.locales").joinpath(
            code, "LC_MESSAGES", "messages.mo"
        )
        with mo_path.open("rb") as f:
            return gettext.GNUTranslations(f)
    except (FileNotFoundError, OSError):
        if code not in _missing_catalogs_warned:
            _missing_catalogs_warned.add(code)
            logger.warning(
                "No compiled translation catalog for '%s'; falling back to "
                "English. Run 'python scripts/compile_mo.py' to build it.",
                code,
            )
        return gettext.NullTranslations()


def reload_catalogs() -> None:
    """Drop cached catalogs so they are re-read from disk on next use."""
    _translations.clear()
    _missing_catalogs_warned.clear()


def t(msgid: str) -> str:
    """
    Translate a message id using the currently active language.

    :param msgid: English source string.
    :return: The translated string, or ``msgid`` when no translation exists.
    """
    return get_translation().gettext(msgid)
