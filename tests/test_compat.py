"""Tests for the compatibility rules and the API manifest.

Run from the skill directory, on every Python the manifest has to support::

    uv run --no-project --python 3.10 --with packaging --with pytest pytest tests

The orchestrator needs tomllib, so its own test is skipped below 3.11.
"""  # noqa: E501

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import textwrap

import pytest

from python_project_tools import api_manifest, compat_rules

OLD_SOURCE: str = """
import enum
import json
from typing import Union

__all__ = ['convert', 'Spam', 'Colour', 'Number', 'helper', 'gone']

Number = Union[int, float]


def convert(value, default=0, *, strict=False, **options):
    return value


def helper(a, b, /, c=1, *args):
    return a


def gone():
    return None


class Base:
    pass


class Spam(Base):
    def __init__(self, eggs, ham=None):
        self.eggs = eggs

    def fry(self, minutes=3):
        return minutes

    def boil(self):
        return 1

    @property
    def size(self):
        return 1

    def __len__(self):
        return 1


class Colour(enum.Enum):
    RED = 1
    GREEN = 2
"""

NEW_SOURCE: str = """
import enum
import typing as _typing

__all__ = ['convert', 'Spam', 'Colour', 'Number', 'helper', 'shiny']

Number = _typing.Union[int, float, complex]


def convert(value, fallback=0, *, strict=True):
    return value


def helper(x, y, /, c=1, *args):
    return x


def shiny():
    return None


class Spam:
    def __init__(self, eggs, ham):
        self.eggs = eggs

    def fry(self, minutes, oil=True):
        return minutes

    @property
    def boil(self):
        return 1

    @property
    def size(self):
        return 1


class Colour(enum.Enum):
    RED = 1
"""


def make_package(root: pathlib.Path, source: str) -> pathlib.Path:
    """Write a one-module package named spamlib under root."""
    package: pathlib.Path = root / 'spamlib'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text(textwrap.dedent(source))
    (package / 'extra.py').write_text('def tool():\n    return 1\n')
    return root


def manifest_of(root: pathlib.Path) -> dict[str, object]:
    """Run api_manifest.py on a package tree, the way check_compat does."""
    done: subprocess.CompletedProcess[str] = subprocess.run(
        [
            sys.executable,
            '-I',
            api_manifest.__file__,
            '--path',
            str(root),
            '--package',
            'spamlib',
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout)


@pytest.fixture(scope='module')
def findings(
    tmp_path_factory: pytest.TempPathFactory,
) -> list[compat_rules.Finding]:
    """Findings between the old and the new spamlib, with extra.py removed from the new one."""  # noqa: E501
    old: pathlib.Path = make_package(
        tmp_path_factory.mktemp('old'), OLD_SOURCE
    )
    new: pathlib.Path = make_package(
        tmp_path_factory.mktemp('new'), NEW_SOURCE
    )
    (new / 'spamlib' / 'extra.py').unlink()
    return compat_rules.diff_manifests(
        'api', manifest_of(old), manifest_of(new)
    )


def lines(findings: list[compat_rules.Finding], severity: str) -> set[str]:
    """Findings of one severity as 'subject: message' strings."""
    return {
        f'{f.subject}: {f.message}' for f in findings if f.severity == severity
    }


@pytest.mark.parametrize(
    'expected',
    [
        'spamlib.convert: `default` can no longer be passed by keyword (removed or renamed)',  # noqa: E501
        'spamlib.convert: `**options` removed',
        'spamlib.gone: removed',
        'spamlib.Base: removed',
        'spamlib.Spam: no longer a subclass of spamlib.Base',
        'spamlib.Spam(): `ham` lost its default and is now required',
        'spamlib.Spam.fry: `minutes` lost its default and is now required',
        'spamlib.Spam.boil: changed from method to property',
        'spamlib.Spam.__len__: member removed',
        'spamlib.Colour.GREEN: enum member removed',
        'spamlib.extra: module removed',
    ],
)
def test_breaks(findings: list[compat_rules.Finding], expected: str) -> None:
    assert expected in lines(findings, 'break')


@pytest.mark.parametrize(
    'expected',
    [
        'spamlib.convert: default of `strict` changed from False to True',
        'spamlib.helper: positional-only parameter 1 renamed from `a` to `x`',
        'spamlib.Number: type alias changed from ',
        'spamlib.Union: re-export of ',
        'spamlib.json: module alias for json removed',
    ],
)
def test_warnings(findings: list[compat_rules.Finding], expected: str) -> None:
    # The reprs of typing objects differ between Python versions, so the
    # alias lines are matched on their stable prefix.
    assert any(line.startswith(expected) for line in lines(findings, 'warn'))


def test_enum_member_reported_once(
    findings: list[compat_rules.Finding],
) -> None:
    assert [
        f.message for f in findings if f.subject == 'spamlib.Colour.GREEN'
    ] == ['enum member removed']


def test_additions(findings: list[compat_rules.Finding]) -> None:
    assert {
        'spamlib.shiny: new public name',
        'spamlib.Spam.fry: new optional parameter `oil`',
    } <= lines(findings, 'add')


def test_nothing_else_breaks(findings: list[compat_rules.Finding]) -> None:
    """Positional-only renames, aliases and re-exports never count as breaks."""  # noqa: E501
    assert not any(
        'helper' in line or 'Number' in line or 'json' in line
        for line in lines(findings, 'break')
    )


def test_identical_trees_have_no_findings(tmp_path: pathlib.Path) -> None:
    root: pathlib.Path = make_package(tmp_path, OLD_SOURCE)
    assert (
        compat_rules.diff_manifests(
            'api', manifest_of(root), manifest_of(root)
        )
        == []
    )


def signature(*params: tuple[str, str, bool]) -> dict[str, object]:
    """A described signature from (name, kind, has_default) triples."""
    return {
        'params': [
            {
                'name': n,
                'kind': k,
                'has_default': d,
                'default': '0' if d else None,
            }
            for n, k, d in params
        ],
        'returns': None,
    }


def test_keyword_only_move_is_a_break() -> None:
    old: dict[str, object] = signature(
        ('a', 'POSITIONAL_OR_KEYWORD', False),
        ('b', 'POSITIONAL_OR_KEYWORD', True),
    )
    new: dict[str, object] = signature(
        ('a', 'POSITIONAL_OR_KEYWORD', False), ('b', 'KEYWORD_ONLY', True)
    )
    found: list[compat_rules.Finding] = compat_rules.diff_signature(
        'api', 'f', old, new
    )
    assert [f.message for f in found if f.severity == 'break'] == [
        '`b` is now keyword-only'
    ]


def test_inserted_parameter_moves_the_next_one() -> None:
    old: dict[str, object] = signature(
        ('a', 'POSITIONAL_OR_KEYWORD', False),
        ('b', 'POSITIONAL_OR_KEYWORD', True),
    )
    new: dict[str, object] = signature(
        ('a', 'POSITIONAL_OR_KEYWORD', False),
        ('x', 'POSITIONAL_OR_KEYWORD', True),
        ('b', 'POSITIONAL_OR_KEYWORD', True),
    )
    found: list[compat_rules.Finding] = compat_rules.diff_signature(
        'api', 'f', old, new
    )
    assert [f.message for f in found if f.severity == 'break'] == [
        '`b` moved from position 2 to 3'
    ]


def test_rename_in_place_only_breaks_keywords() -> None:
    old: dict[str, object] = signature(
        ('a', 'POSITIONAL_OR_KEYWORD', False),
        ('b', 'POSITIONAL_OR_KEYWORD', True),
    )
    new: dict[str, object] = signature(
        ('a', 'POSITIONAL_OR_KEYWORD', False),
        ('c', 'POSITIONAL_OR_KEYWORD', True),
    )
    found: list[compat_rules.Finding] = compat_rules.diff_signature(
        'api', 'f', old, new
    )
    assert [f.message for f in found if f.severity == 'break'] == [
        '`b` can no longer be passed by keyword (removed or renamed)'
    ]


def test_new_required_parameter_is_a_break() -> None:
    old: dict[str, object] = signature(('a', 'POSITIONAL_OR_KEYWORD', False))
    new: dict[str, object] = signature(
        ('a', 'POSITIONAL_OR_KEYWORD', False), ('b', 'KEYWORD_ONLY', False)
    )
    found: list[compat_rules.Finding] = compat_rules.diff_signature(
        'api', 'f', old, new
    )
    assert [f.message for f in found] == ['new required parameter `b`']


def test_kwargs_absorbs_a_removed_keyword() -> None:
    old: dict[str, object] = signature(('a', 'KEYWORD_ONLY', True))
    new: dict[str, object] = signature(('kwargs', 'VAR_KEYWORD', False))
    assert compat_rules.diff_signature('api', 'f', old, new) == []


def metadata(
    python: str | None,
    deps: list[str],
    extras: list[str] | None = None,
    points: dict[str, dict[str, str]] | None = None,
) -> dict[str, object]:
    """Wheel metadata in the shape check_compat reads it."""
    return {
        'requires_python': python,
        'requires_dist': deps,
        'extras': extras or [],
        'entry_points': points or {},
    }


def test_metadata_rules() -> None:
    old: dict[str, object] = metadata(
        '>=3.9',
        ['eggs>=1.0', 'ham', 'toast>=2; extra == "fast"'],
        ['fast', 'slow'],
        {'console_scripts': {'spam': 'spamlib:main', 'fry': 'spamlib:fry'}},
    )
    new: dict[str, object] = metadata(
        '>=3.11',
        ['eggs>=1.4', 'ham<3', 'bacon', 'toast>=2; extra == "fast"'],
        ['fast'],
        {'console_scripts': {'spam': 'spamlib.cli:main'}},
    )
    found: set[str] = {
        f'{f.severity} {f.subject}: {f.message}'
        for f in compat_rules.diff_metadata(old, new)
    }
    assert found == {
        'break Requires-Python: drops Python 3.9, 3.10 (>=3.9 -> >=3.11)',
        'break eggs: minimum raised from 1.0 to 1.4',
        'warn ham: now capped: <3',
        'warn bacon: new required dependency: bacon',
        'break extra slow: extra removed',
        'break console_scripts:fry: entry point removed',
        'warn console_scripts:spam: now points to spamlib.cli:main, was spamlib:main',  # noqa: E501
    }


def test_griffe_line() -> None:
    pytest.importorskip('tomllib')
    from python_project_tools import check_compat

    assert check_compat.griffe_line(
        'src/spamlib/core.py:12: convert(default): Parameter was removed'
    ) == ('spamlib.core.convert', 'convert(default): Parameter was removed')
    assert check_compat.griffe_line(
        'spamlib/__init__.py:3: KT: Attribute value was changed: a -> b'
    ) == ('spamlib.KT', 'Attribute value was changed: a -> b')


def test_check_compat_is_registered() -> None:
    from python_project_tools import check_compat, cli

    assert cli.COMMANDS['check-compat'] is check_compat.main
