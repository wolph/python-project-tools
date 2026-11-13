"""Tests for the bundle identity helper."""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

from python_project_tools import bundle_verifier, signing


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


def test_main_rejects_bad_sha256(tmp_path: pathlib.Path) -> None:
    manifest: pathlib.Path = tmp_path / 'manifest.json'
    manifest.write_text(
        json.dumps([{'name': 'a', 'sha256': 'abc', 'bundle': 'b.json'}])
    )
    output: pathlib.Path = tmp_path / 'out.json'
    code: int = bundle_verifier.main(
        [
            '--manifest',
            str(manifest),
            '--repo',
            'a/b',
            '--output',
            str(output),
        ]
    )
    assert code == 2
    assert not output.exists()


def test_ci_policy_in_sigstore_environment() -> None:
    if shutil.which('uvx') is None:
        pytest.skip('uvx is not installed')
    data: pathlib.Path = pathlib.Path(__file__).parent / 'data'
    done: subprocess.CompletedProcess[str] = subprocess.run(
        [
            *signing.SIGSTORE_ARGV,
            str(data / 'policy_cases.py'),
            bundle_verifier.__file__,
            str(data / 'pypi-provenance-python-utils-4.2.1.json'),
        ],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {
        'real_publish_cert': False,
        'sign_yml': True,
        'sign_yml_source_other_case': True,
        'wrong_repo': False,
        'wrong_source_repo': False,
        'no_source_repo': False,
        'email_san': False,
        'push_trigger': False,
        'wrong_issuer': False,
    }
