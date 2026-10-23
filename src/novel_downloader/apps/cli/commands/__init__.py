#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands
----------------------------------

CLI command definitions. Each file corresponds to a subcommand.
"""

__all__ = ["commands"]

from .clean import CleanCmd
from .config import ConfigCmd
from .download import DownloadCmd
from .export import ExportCmd
from .login import LoginCmd
from .music import MusicCmd
from .search import SearchCmd
from .sources import SourcesCmd

commands = [
    DownloadCmd,
    SearchCmd,
    ExportCmd,
    LoginCmd,
    ConfigCmd,
    CleanCmd,
    SourcesCmd,
    MusicCmd,
]
