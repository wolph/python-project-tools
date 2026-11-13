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
            'a.whl', 'aa' * 32, tmp_path / 'a.whl.sigstore.json'
        ),
        identities.BundleCheck(
            'a.tar.gz', 'bb' * 32, tmp_path / 'a.tar.gz.sigstore.json'
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
                'sha256': 'aa' * 32,
                'bundle': str(tmp_path / 'a.whl.sigstore.json'),
            },
            {
                'name': 'a.tar.gz',
                'sha256': 'bb' * 32,
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
            [identities.BundleCheck('a', 'aa' * 32, tmp_path / 'a.json')],
            'WoLpH/x',
        )


def _tissue_bundle(directory: pathlib.Path) -> tuple[pathlib.Path, str]:
    """The backfilled bundle of tissue 0.9.2 and the sha256 of its sdist.

    Skips the test when gh, uvx or the network is unavailable.
    """
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
            str(directory),
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
    return directory / f'{name}.sigstore.json', digest


def test_real_backfilled_bundle_verifies_as_rick(
    tmp_path: pathlib.Path,
) -> None:
    bundle: pathlib.Path
    digest: str
    bundle, digest = _tissue_bundle(tmp_path)
    result: dict[str, identities.Identity | None] = identities.verify_bundles(
        [identities.BundleCheck('tissue', digest, bundle)], 'WoLpH/tissue'
    )
    assert result == {'tissue': 'rick'}


def test_real_bundle_with_other_digest_or_junk_gives_none(
    tmp_path: pathlib.Path,
) -> None:
    bundle: pathlib.Path
    digest: str
    bundle, digest = _tissue_bundle(tmp_path)
    junk: pathlib.Path = tmp_path / 'junk.sigstore.json'
    junk.write_text('{"not": "a bundle"}')
    result: dict[str, identities.Identity | None] = identities.verify_bundles(
        [
            identities.BundleCheck('right', digest, bundle),
            identities.BundleCheck('other-digest', '00' * 32, bundle),
            identities.BundleCheck('junk', digest, junk),
        ],
        'WoLpH/tissue',
    )
    assert result == {'right': 'rick', 'other-digest': None, 'junk': None}


def test_verify_bundles_rejects_duplicate_names(
    tmp_path: pathlib.Path,
) -> None:
    check: identities.BundleCheck = identities.BundleCheck(
        'a', '00' * 32, tmp_path / 'a.json'
    )
    with pytest.raises(ValueError, match='duplicate'):
        identities.verify_bundles([check, check], 'WoLpH/x')


@pytest.mark.parametrize('sha256', ['aa', 'zz' * 32, '00' * 33, ''])
def test_bundle_check_rejects_bad_sha256(sha256: str) -> None:
    with pytest.raises(ValueError, match='64 hex'):
        identities.BundleCheck('a', sha256, pathlib.Path('a.json'))


@pytest.mark.parametrize(
    'output',
    [
        {'a': 'ci', 'extra': None},
        {},
        {'a': 'admin'},
        {'a': True},
        ['a'],
    ],
)
def test_verify_bundles_rejects_unexpected_helper_output(
    output: object, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def write_output(argv: list[str]) -> None:
        pathlib.Path(_argument(argv, '--output')).write_text(
            json.dumps(output)
        )

    recorder: Recorder = Recorder(
        {PREFIX: completed()}, hooks={PREFIX: write_output}
    )
    monkeypatch.setattr(common, 'run', recorder)
    with pytest.raises(signing.SigningError, match='unexpected output'):
        identities.verify_bundles(
            [identities.BundleCheck('a', '00' * 32, tmp_path / 'a.json')],
            'WoLpH/x',
        )
