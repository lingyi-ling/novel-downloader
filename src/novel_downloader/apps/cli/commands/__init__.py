#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands
----------------------------------

CLI command definitions. Each file corresponds to a subcommand.
"""

__all__ = ["commands"]

from .clean import CleanCmd
from .completion import CompletionCmd
from .config import ConfigCmd
from .doctor import DoctorCmd
from .download import DownloadCmd
from .export import ExportCmd
from .follow import FollowCmd
from .login import LoginCmd
from .music import MusicCmd
from .search import SearchCmd
from .sources import SourcesCmd
from .storage import StorageCmd

commands = [
    DownloadCmd,
    SearchCmd,
    FollowCmd,
    ExportCmd,
    LoginCmd,
    ConfigCmd,
    CleanCmd,
    StorageCmd,
    DoctorCmd,
    SourcesCmd,
    MusicCmd,
    CompletionCmd,
]
