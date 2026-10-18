#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.base
---------------------------------------

"""

from abc import ABC, abstractmethod
from argparse import ArgumentParser, Namespace, _SubParsersAction

from novel_downloader.infra.i18n import t
from novel_downloader.schemas import BookConfig

__all__ = ["Command", "localize_help", "parse_book_args"]


def localize_help(parser: ArgumentParser) -> None:
    """
    Add a localized ``-h`` / ``--help`` action to ``parser``.

    Parsers must be created with ``add_help=False`` so the built-in (English)
    ``-h`` action can be replaced by this one.

    :param parser: Parser to add the help action to.
    """
    parser.add_argument(
        "-h",
        "--help",
        action="help",
        help=t("Show this help message and exit."),
    )


def parse_book_args(
    book_ids: list[str],
    start_id: str | None,
    end_id: str | None,
) -> list[BookConfig]:
    """
    Convert CLI ``book_ids`` / ``--start`` / ``--end`` into ``BookConfig`` items.

    ``--start`` / ``--end`` only ever apply to the first book, matching the
    documented CLI contract.

    :param book_ids: Book IDs (or URLs) in command-line order.
    :param start_id: Start chapter ID, applied to the first book only.
    :param end_id: End chapter ID, applied to the first book only.
    :return: One ``BookConfig`` per book ID (empty list when none were given).
    """
    if not book_ids:
        return []

    result: list[BookConfig] = [
        BookConfig(
            book_id=book_ids[0],
            start_id=start_id,
            end_id=end_id,
        )
    ]

    result.extend(BookConfig(book_id=book_id) for book_id in book_ids[1:])

    return result


class Command(ABC):
    name: str
    help: str

    @classmethod
    def register(cls, subparsers: "_SubParsersAction[ArgumentParser]") -> None:
        parser = subparsers.add_parser(
            cls.name,
            help=cls.help,
            # Repeat the one-line summary as the subcommand description so
            # ``<command> --help`` explains itself instead of starting blank.
            description=cls.help,
            add_help=False,
        )
        localize_help(parser)
        cls.add_arguments(parser)
        parser.set_defaults(func=cls.run)

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        return

    @classmethod
    @abstractmethod
    def run(cls, args: Namespace) -> None:
        pass
