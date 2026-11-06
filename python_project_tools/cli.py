"""The ``ppt`` command line dispatcher."""

import argparse
from collections.abc import Callable

from python_project_tools import __version__

COMMANDS: dict[str, Callable[[list[str]], int]] = {}


def main(argv: list[str] | None = None) -> int:
    """Run the subcommand named in ``argv`` and return its exit status."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(prog='ppt')
    parser.add_argument(
        '--version',
        action='version',
        version=f'ppt {__version__}',
    )
    parser.add_argument('command', choices=sorted(COMMANDS))
    args: argparse.Namespace
    rest: list[str]
    args, rest = parser.parse_known_args(argv)
    return COMMANDS[args.command](rest)
