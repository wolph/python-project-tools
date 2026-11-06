"""Tests for tag normalisation and PEP 440 helpers."""

from __future__ import annotations

import pytest
from packaging.version import Version

from python_project_tools import versions
from python_project_tools.github import Tag


@pytest.mark.parametrize(
    ('tag', 'expected'),
    [
        ('v1.2.3', '1.2.3'),
        ('1.2.3', '1.2.3'),
        ('release-1_2', '1.2'),
        ('vv2.0.0', 'v2.0.0'),
        ('untagged-7f3a', 'untagged.7f3a'),
    ],
)
def test_normalise(tag: str, expected: str) -> None:
    assert versions.normalise(tag) == expected


@pytest.mark.parametrize(
    ('tag', 'ok'),
    [
        ('v1.0', True),
        ('ci', False),
        ('vv2.0.0', False),
        ('untagged-7f3a', False),
        ('4.17.0', True),
    ],
)
def test_is_version_tag(tag: str, ok: bool) -> None:
    assert versions.is_version_tag(tag) is ok


def test_match_tag_prefers_exact_then_normalised() -> None:
    tags: list[Tag] = [Tag('1.0', 'a'), Tag('v1.0', 'b')]
    assert versions.match_tag('1.0', tags) == Tag('1.0', 'a')
    assert versions.match_tag('2.0', tags) is None
    assert versions.match_tag('1.0', tags[1:]) == Tag('v1.0', 'b')


@pytest.mark.parametrize(
    ('v', 'pre'),
    [
        ('1.0', False),
        ('1.0rc1', True),
        ('1.0.dev3', True),
        ('1.0.post1', False),
    ],
)
def test_is_prerelease(v: str, pre: bool) -> None:
    assert versions.is_prerelease(v) is pre


def test_tag_for_and_sort_key() -> None:
    assert versions.tag_for('1.2') == 'v1.2'
    assert versions.sort_key('1.0') == Version('1.0')
    assert sorted(['1.10', '1.9'], key=versions.sort_key) == ['1.9', '1.10']
