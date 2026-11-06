"""The ``ppt`` command line dispatcher."""

import argparse
import sys
from collections.abc import Callable

from python_project_tools import (
    __version__,
    backfill,
    check_compat,
    conda_forge,
    inventory,
    match_tag,
    sign,
    verify_release,
)

COMMANDS: dict[str, Callable[[list[str]], int]] = {
    'backfill': backfill.main,
    'check-compat': check_compat.main,
    'conda-forge': conda_forge.main,
    'inventory': inventory.main,
    'match-tag': match_tag.main,
    'sign': sign.main,
    'verify-release': verify_release.main,
}


def main(argv: list[str] | None = None) -> int:
    """Run the subcommand named in ``argv`` and return its exit status."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog='ppt',
        allow_abbrev=False,
    )
    parser.add_argument(
        '--version',
        action='version',
        version=f'ppt {__version__}',
    )
    parser.add_argument('command', choices=sorted(COMMANDS))
    tokens: list[str] = sys.argv[1:] if argv is None else argv
    args: argparse.Namespace = parser.parse_args(tokens[:1])
    return COMMANDS[args.command](tokens[1:])
