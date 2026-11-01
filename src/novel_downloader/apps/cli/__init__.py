#!/usr/bin/env python3
"""
novel_downloader.apps.cli
-------------------------

Command-line interface layer built on top of rich.

``cli_main`` is resolved lazily (PEP 562) on purpose: importing it eagerly
here made ``python -m novel_downloader.apps.cli.main`` execute the module
twice -- runpy first imports this package, the eager import then loaded
``.main`` into ``sys.modules``, and runpy ran it again as ``__main__``,
printing a ``RuntimeWarning`` on every invocation. Resolving the attribute
on demand keeps the ``-m`` run single-execution while
``from novel_downloader.apps.cli import cli_main`` (the path
``novel_downloader.__main__`` uses) keeps working unchanged.
"""

from typing import Any

__all__ = ["cli_main"]


def __getattr__(name: str) -> Any:
    """Import :func:`cli_main` from ``.main`` on first attribute access."""
    if name == "cli_main":
        from .main import cli_main

        return cli_main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
