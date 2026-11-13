"""Tests for the bundle identity check."""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import typing
import urllib.error
import urllib.request

import pytest

from python_project_tools import common, identities, signing
from tests.fakes import Recorder, completed

PREFIX: str = 'uvx --from sigstore==4.5.1 python -I'


def _argument(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_verify_bundles_runs_helper_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[dict[str, str]]] = []

    def write_output(argv: list[str]) -> None:
        seen.append(
            json.loads(pathlib.Path(_argument(argv, '--manifest')).read_text())
        )
        pathlib.Path(_argument(argv, '--output')).write_text(
            json.dumps({'a.whl': 'ci', 'a.tar.gz': None})
        )

    recorder: Recorder = Recorder(
        {PREFIX: completed()}, hooks={PREFIX: write_output}
    )
    monkeypatch.setattr(common, 'run', recorder)
    checks: list[identities.BundleCheck] = [
        identities.BundleCheck(
            'a.whl', 'aa', tmp_path / 'a.whl.sigstore.json'
        ),
        identities.BundleCheck(
            'a.tar.gz', 'bb', tmp_path / 'a.tar.gz.sigstore.json'
        ),
    ]
    result: dict[str, identities.Identity | None] = identities.verify_bundles(
        checks, 'WoLpH/x'
    )
    assert result == {'a.whl': 'ci', 'a.tar.gz': None}
    assert len(recorder.calls) == 1
    argv: list[str] = recorder.calls[0]
    assert argv[: len(signing.SIGSTORE_ARGV)] == signing.SIGSTORE_ARGV
    assert str(identities.VERIFIER_SCRIPT) in argv
    assert _argument(argv, '--repo') == 'WoLpH/x'
    assert seen == [
        [
            {
                'name': 'a.whl',
                'sha256': 'aa',
                'bundle': str(tmp_path / 'a.whl.sigstore.json'),
            },
            {
                'name': 'a.tar.gz',
                'sha256': 'bb',
                'bundle': str(tmp_path / 'a.tar.gz.sigstore.json'),
            },
        ]
    ]


def test_verify_bundles_raises_on_helper_failure(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder: Recorder = Recorder(
        {PREFIX: completed(returncode=1, stderr='boom')}
    )
    monkeypatch.setattr(common, 'run', recorder)
    with pytest.raises(signing.SigningError, match='boom'):
        identities.verify_bundles(
            [identities.BundleCheck('a', 'aa', tmp_path / 'a.json')], 'WoLpH/x'
        )


def test_real_backfilled_bundle_verifies_as_rick(
    tmp_path: pathlib.Path,
) -> None:
    if shutil.which('gh') is None or shutil.which('uvx') is None:
        pytest.skip('gh or uvx is not installed')
    name: str = 'tissue-0.9.2.tar.gz'
    download: subprocess.CompletedProcess[str] = subprocess.run(
        [
            'gh',
            'release',
            'download',
            'v0.9.2',
            '--repo',
            'WoLpH/tissue',
            '--pattern',
            f'{name}.sigstore.json',
            '--dir',
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )
    if download.returncode != 0:
        pytest.skip(f'cannot download the bundle: {download.stderr.strip()}')
    try:
        with urllib.request.urlopen(
            'https://pypi.org/pypi/tissue/0.9.2/json', timeout=30
        ) as response:
            urls: list[dict[str, typing.Any]] = json.load(response)['urls']
    except (urllib.error.URLError, TimeoutError) as error:
        pytest.skip(f'cannot reach PyPI: {error}')
    digest: str = next(
        str(url['digests']['sha256'])
        for url in urls
        if url['filename'] == name
    )
    result: dict[str, identities.Identity | None] = identities.verify_bundles(
        [
            identities.BundleCheck(
                name, digest, tmp_path / f'{name}.sigstore.json'
            )
        ],
        'WoLpH/tissue',
    )
    assert result == {name: 'rick'}
