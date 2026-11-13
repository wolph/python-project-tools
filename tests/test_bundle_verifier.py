"""Tests for the bundle identity helper."""

from __future__ import annotations

import pytest

from python_project_tools import bundle_verifier


@pytest.mark.parametrize(
    ('san', 'expected'),
    [
        (
            'https://github.com/WoLpH/python-utils/.github/workflows/sign.yml@refs/heads/develop',
            True,
        ),
        (
            'https://github.com/WoLpH/python-utils/.github/workflows/sign.yml@refs/heads/master',
            True,
        ),
        (
            'https://github.com/wolph/PYTHON-UTILS/.github/workflows/sign.yml@refs/heads/develop',
            True,
        ),
        (
            'https://github.com/WoLpH/python-utils/.github/workflows/publish.yml@refs/heads/develop',
            False,
        ),
        (
            'https://github.com/WoLpH/python-utils/.github/workflows/sign.yml@refs/tags/v4.2.1',
            False,
        ),
        (
            'https://github.com/WoLpH/python-utils/.github/workflows/sign.yml@refs/heads/',
            False,
        ),
        (
            'https://github.com/WoLpH/numpy-stl/.github/workflows/sign.yml@refs/heads/develop',
            False,
        ),
        (
            'https://github.com/WoLpH/python-utils-evil/.github/workflows/sign.yml@refs/heads/develop',
            False,
        ),
    ],
)
def test_ci_identity_matches(san: str, expected: bool) -> None:
    assert (
        bundle_verifier.ci_identity_matches(san, 'WoLpH/python-utils')
        is expected
    )


def test_main_rejects_missing_arguments() -> None:
    assert bundle_verifier.main(['--repo', 'a/b']) == 2
