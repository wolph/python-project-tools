"""Tests for gpg, sigstore and SHA256SUMS handling."""

from __future__ import annotations

import collections.abc
import pathlib
import shutil
import subprocess
import tempfile

import pytest

from python_project_tools import common, signing
from tests.fakes import Recorder, completed

needs_gpg = pytest.mark.skipif(
    shutil.which('gpg') is None, reason='gpg is not installed'
)


@pytest.fixture
def gnupg_home(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> collections.abc.Iterator[str]:
    """A throwaway GNUPGHOME with one ed25519 key, yielding its fingerprint."""
    # gpg-agent sockets live under GNUPGHOME and unix socket paths cap at
    # about 104 bytes, which pytest's tmp_path on macOS already exceeds.
    # mkdtemp gives a short directory with mode 0o700.
    home: pathlib.Path = pathlib.Path(tempfile.mkdtemp(prefix='ppt-gpg-'))
    monkeypatch.setenv('GNUPGHOME', str(home))
    subprocess.run(
        [
            'gpg',
            '--batch',
            '--passphrase',
            '',
            '--quick-generate-key',
            'ppt test <ppt@example.invalid>',
            'ed25519',
            'sign',
            '1d',
        ],
        check=True,
        capture_output=True,
    )
    listing: str = subprocess.run(
        ['gpg', '--batch', '--list-secret-keys', '--with-colons'],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    fingerprint: str = next(
        line.split(':')[9]
        for line in listing.splitlines()
        if line.startswith('fpr:')
    )
    try:
        yield fingerprint
    finally:
        subprocess.run(['gpgconf', '--kill', 'gpg-agent'], check=False)
        shutil.rmtree(home, ignore_errors=True)


@needs_gpg
def test_gpg_sign_and_verify_round_trip(
    gnupg_home: str, tmp_path: pathlib.Path
) -> None:
    target: pathlib.Path = tmp_path / 'a.whl'
    target.write_bytes(b'wheel bytes')
    signature: pathlib.Path = signing.gpg_sign(target, gnupg_home)
    assert signature == tmp_path / 'a.whl.asc'
    assert signature.read_text().startswith('-----BEGIN PGP SIGNATURE-----')
    assert signing.gpg_verify(target, signature) is True


@needs_gpg
def test_gpg_verify_fails_on_changed_file(
    gnupg_home: str, tmp_path: pathlib.Path
) -> None:
    target: pathlib.Path = tmp_path / 'a.whl'
    target.write_bytes(b'wheel bytes')
    signature: pathlib.Path = signing.gpg_sign(target, gnupg_home)
    target.write_bytes(b'tampered')
    assert signing.gpg_verify(target, signature) is False


@needs_gpg
def test_gpg_sign_raises_for_unknown_key(
    gnupg_home: str, tmp_path: pathlib.Path
) -> None:
    target: pathlib.Path = tmp_path / 'a.whl'
    target.write_bytes(b'x')
    with pytest.raises(signing.SigningError, match='gpg failed'):
        signing.gpg_sign(target, 'DEADBEEFDEADBEEFDEADBEEFDEADBEEFDEADBEEF')


def _sigstore_fake(
    skip: str = '',
) -> tuple[Recorder, collections.abc.Callable[..., object]]:
    """A Recorder plus a run replacement that also writes the bundles."""
    rec: Recorder = Recorder({'uvx sigstore sign': completed()})

    def fake_run(
        args: list[str],
        check: bool = False,
        cwd: pathlib.Path | None = None,
    ) -> object:
        reply: object = rec(args, check=check, cwd=cwd)
        for name in args[args.index('--overwrite') + 1 :]:
            if skip and name.endswith(skip):
                continue
            pathlib.Path(name + '.sigstore.json').write_text('{}')
        return reply

    return rec, fake_run


def test_sigstore_sign_invokes_once_for_all_files(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec, fake_run = _sigstore_fake()
    monkeypatch.setattr(common, 'run', fake_run)
    wheel: pathlib.Path = tmp_path / 'a.whl'
    sdist: pathlib.Path = tmp_path / 'a.tar.gz'
    out: list[pathlib.Path] = signing.sigstore_sign([wheel, sdist])
    assert rec.calls == [
        [
            *signing.SIGSTORE_ARGV,
            'sign',
            '--output-directory',
            str(tmp_path),
            '--overwrite',
            str(wheel),
            str(sdist),
        ]
    ]
    assert [p.name for p in out] == [
        'a.whl.sigstore.json',
        'a.tar.gz.sigstore.json',
    ]
    assert all(p.exists() for p in out)


def test_sigstore_sign_raises_when_bundle_missing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fake_run = _sigstore_fake(skip='a.tar.gz')
    monkeypatch.setattr(common, 'run', fake_run)
    with pytest.raises(signing.SigningError, match=r'a\.tar\.gz\.sigstore'):
        signing.sigstore_sign([tmp_path / 'a.whl', tmp_path / 'a.tar.gz'])


def test_sigstore_sign_rejects_mixed_directories(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec, fake_run = _sigstore_fake()
    monkeypatch.setattr(common, 'run', fake_run)
    with pytest.raises(signing.SigningError, match='one directory'):
        signing.sigstore_sign([tmp_path / 'a.whl', tmp_path / 'sub' / 'b.whl'])
    assert rec.calls == []


def test_sigstore_sign_raises_on_nonzero_exit(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec: Recorder = Recorder({'uvx sigstore sign': completed(returncode=1)})
    monkeypatch.setattr(common, 'run', rec)
    with pytest.raises(signing.SigningError, match='sigstore failed'):
        signing.sigstore_sign([tmp_path / 'a.whl'])


def test_sha256sums_format_and_rebuilt_comment(tmp_path: pathlib.Path) -> None:
    a: pathlib.Path = tmp_path / 'a.whl'
    b: pathlib.Path = tmp_path / 'b.tar.gz'
    a.write_bytes(b'one')
    b.write_bytes(b'two')
    sha_a: str = common.sha256_of(a)
    sha_b: str = common.sha256_of(b)
    comment: str = '# rebuilt from tag v1.0 on 2026-11-06, not the PyPI bytes'
    sums: pathlib.Path = signing.write_sha256sums(
        tmp_path, [a, b], {'b.tar.gz': comment}
    )
    assert sums == tmp_path / 'SHA256SUMS'
    text: str = sums.read_text()
    assert text.splitlines() == [
        f'{sha_a}  a.whl',
        comment,
        f'{sha_b}  b.tar.gz',
    ]
    assert text.endswith('\n')
    assert signing.parse_sha256sums(sums) == {
        'a.whl': sha_a,
        'b.tar.gz': sha_b,
    }


def test_parse_sha256sums_ignores_blank_and_comment_lines(
    tmp_path: pathlib.Path,
) -> None:
    sums: pathlib.Path = tmp_path / 'SHA256SUMS'
    sums.write_text('# note\n\n' + 'ab' * 32 + '  x y.whl\n')
    assert signing.parse_sha256sums(sums) == {'x y.whl': 'ab' * 32}
