#!/usr/bin/env python3
"""
novel_downloader.infra.config
-----------------------------

Unified interface for loading and adapting configuration files.
"""

__all__ = [
    "copy_default_config",
    "get_config_value",
    "load_config",
    "migrate_config",
    "validate_config",
    "ConfigAdapter",
    "Problem",
]

from .adapter import ConfigAdapter
from .file_io import (
    copy_default_config,
    get_config_value,
    load_config,
)
from .validation import Problem, migrate_config, validate_config
