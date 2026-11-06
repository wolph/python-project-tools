"""Tests for gpg, sigstore and SHA256SUMS handling."""

from __future__ import annotations

import collections.abc
import pathlib
import shutil
import subprocess
import typing

import pytest

from python_project_tools import common, signing
from tests.fakes import Recorder, completed

needs_gpg = pytest.mark.skipif(
    shutil.which('gpg') is None, reason='gpg is not installed'
)


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
    rec: Recorder = Recorder({'uvx sigstore==4.5.1 sign': completed()})

    def fake_run(
        args: list[str],
        check: bool = False,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        capture: bool = True,
        timeout: float | None = None,
    ) -> object:
        # The OIDC prompt must reach the terminal.
        assert capture is False
        assert timeout == signing.SIGSTORE_TIMEOUT_SECONDS
        # The login URL is routed through the relay so it reaches the log.
        assert env == {'BROWSER': signing.relay_browser()}
        reply: object = rec(args, check=check, cwd=cwd, timeout=timeout)
        out: pathlib.Path = pathlib.Path(
            args[args.index('--output-directory') + 1]
        )
        for name in args[args.index('--overwrite') + 1 :]:
            if skip and name.endswith(skip):
                continue
            source: pathlib.Path = pathlib.Path(name)
            (out / (source.name + '.sigstore.json')).write_text(
                f'bundle of {source.read_bytes().decode()}'
            )
        return reply

    return rec, fake_run


def test_sigstore_sign_invokes_once_for_all_files(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec, fake_run = _sigstore_fake()
    monkeypatch.setattr(common, 'run', fake_run)
    wheel: pathlib.Path = tmp_path / 'a.whl'
    sdist: pathlib.Path = tmp_path / 'a.tar.gz'
    wheel.write_bytes(b'wheel')
    sdist.write_bytes(b'sdist')
    out: list[pathlib.Path] = signing.sigstore_sign([wheel, sdist])
    assert signing.SIGSTORE_ARGV == ['uvx', 'sigstore==4.5.1']
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
    (tmp_path / 'a.whl').write_bytes(b'wheel')
    (tmp_path / 'a.tar.gz').write_bytes(b'sdist')
    with pytest.raises(signing.SigningError, match=r'a\.tar\.gz\.sigstore'):
        signing.sigstore_sign([tmp_path / 'a.whl', tmp_path / 'a.tar.gz'])


def test_sigstore_sign_stages_files_from_several_directories(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec, fake_run = _sigstore_fake()
    monkeypatch.setattr(common, 'run', fake_run)
    first: pathlib.Path = tmp_path / 'v1.0' / 'a.whl'
    second: pathlib.Path = tmp_path / 'v2.0' / 'b.whl'
    for path in (first, second):
        path.parent.mkdir()
        path.write_bytes(path.name.encode())
    out: list[pathlib.Path] = signing.sigstore_sign([first, second])
    assert len(rec.calls) == 1
    argv: list[str] = rec.calls[0]
    staging: pathlib.Path = pathlib.Path(
        argv[argv.index('--output-directory') + 1]
    )
    assert staging not in (first.parent, second.parent)
    assert argv[argv.index('--overwrite') + 1 :] == [
        str(staging / 'a.whl'),
        str(staging / 'b.whl'),
    ]
    assert not staging.exists()
    assert out == [
        tmp_path / 'v1.0' / 'a.whl.sigstore.json',
        tmp_path / 'v2.0' / 'b.whl.sigstore.json',
    ]
    assert out[0].read_text() == 'bundle of a.whl'
    assert out[1].read_text() == 'bundle of b.whl'


def test_sigstore_sign_refuses_duplicate_names_across_directories(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec, fake_run = _sigstore_fake()
    monkeypatch.setattr(common, 'run', fake_run)
    with pytest.raises(signing.SigningError, match='unique file names'):
        signing.sigstore_sign([tmp_path / 'a.whl', tmp_path / 'sub' / 'a.whl'])
    assert rec.calls == []


def test_sigstore_sign_raises_on_nonzero_exit(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec: Recorder = Recorder(
        {'uvx sigstore==4.5.1 sign': completed(returncode=1)}
    )
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


@needs_gpg
def test_gpg_sign_keeps_double_extension(
    gnupg_home: str, tmp_path: pathlib.Path
) -> None:
    target: pathlib.Path = tmp_path / 'a.tar.gz'
    target.write_bytes(b'sdist bytes')
    signature: pathlib.Path = signing.gpg_sign(target, gnupg_home)
    assert signature.name == 'a.tar.gz.asc'
    assert signature.exists()


def _second_key(name: str) -> str:
    """Generate another key in the fixture's GNUPGHOME, return its fpr."""
    subprocess.run(
        [
            'gpg',
            '--batch',
            '--passphrase',
            '',
            '--quick-generate-key',
            f'{name} <{name}@example.invalid>',
            'ed25519',
            'sign',
            '1d',
        ],
        check=True,
        capture_output=True,
    )
    fingerprint: str | None = signing.gpg_fingerprint(
        f'{name}@example.invalid'
    )
    assert fingerprint is not None
    return fingerprint


@needs_gpg
def test_gpg_verify_key_accepts_a_good_signature_by_the_key(
    gnupg_home: str, tmp_path: pathlib.Path
) -> None:
    sums: pathlib.Path = tmp_path / 'SHA256SUMS'
    sums.write_text('ab' * 32 + '  spam.whl\n')
    signature: pathlib.Path = signing.gpg_sign(sums, gnupg_home)
    assert signing.gpg_verify_key(sums, signature, gnupg_home) is True
    # A user ID that names exactly one key resolves to its fingerprint.
    assert signing.gpg_verify_key(sums, signature, 'ppt@example.invalid')
    assert signing.gpg_fingerprint(gnupg_home.lower()) == gnupg_home


@needs_gpg
def test_gpg_verify_key_rejects_a_tampered_file(
    gnupg_home: str, tmp_path: pathlib.Path
) -> None:
    sums: pathlib.Path = tmp_path / 'SHA256SUMS'
    sums.write_text('ab' * 32 + '  spam.whl\n')
    signature: pathlib.Path = signing.gpg_sign(sums, gnupg_home)
    sums.write_text('cd' * 32 + '  spam.whl\n')
    assert signing.gpg_verify_key(sums, signature, gnupg_home) is False


@needs_gpg
def test_gpg_verify_key_rejects_a_signature_by_another_key(
    gnupg_home: str, tmp_path: pathlib.Path
) -> None:
    sums: pathlib.Path = tmp_path / 'SHA256SUMS'
    sums.write_text('ab' * 32 + '  spam.whl\n')
    signature: pathlib.Path = signing.gpg_sign(sums, gnupg_home)
    other: str = _second_key('eggs')
    assert other != gnupg_home
    assert signing.gpg_verify_key(sums, signature, other) is False
    unknown: str = 'DEADBEEF' * 5
    assert signing.gpg_verify_key(sums, signature, unknown) is False
    # 'example.invalid' now names two keys, so it names none.
    assert signing.gpg_fingerprint('example.invalid') is None


def test_signed_by_needs_goodsig_and_the_fingerprint() -> None:
    primary: str = 'A' * 40
    subkey: str = 'B' * 40
    valid: list[str] = ['VALIDSIG', subkey, '2026-11-06', *'1234567', primary]
    good: list[str] = ['GOODSIG', 'A' * 16, 'spam']
    assert signing._signed_by([good, valid], primary) is True
    assert signing._signed_by([good, valid], subkey) is True
    assert signing._signed_by([good, valid], 'C' * 40) is False
    assert signing._signed_by([valid], primary) is False
    expired: list[str] = ['EXPKEYSIG', 'A' * 16, 'spam']
    assert signing._signed_by([good, expired, valid], primary) is False


def test_sigstore_sign_times_out_with_a_clear_error(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[float | None] = []

    def hanging(args: list[str], **kwargs: typing.Any) -> object:
        seen.append(kwargs.get('timeout'))
        raise subprocess.TimeoutExpired(args, kwargs['timeout'])

    monkeypatch.setattr(common, 'run', hanging)
    with pytest.raises(RuntimeError) as raised:
        signing.sigstore_sign([tmp_path / 'a.whl', tmp_path / 'b.whl'])
    assert str(raised.value) == (
        'sigstore did not finish within 900 s, the browser login probably '
        'never reached it'
    )
    assert seen == [900]
    assert capsys.readouterr().err == (
        'ppt: Sigstore login needed, a browser tab opens for 2 files\n'
    )


def test_sha256sums_comments_are_the_rebuilt_lines(
    tmp_path: pathlib.Path,
) -> None:
    a: pathlib.Path = tmp_path / 'a.whl'
    b: pathlib.Path = tmp_path / 'b.tar.gz'
    a.write_bytes(b'one')
    b.write_bytes(b'two')
    comment: str = '# rebuilt from tag v1.0 on 2026-11-06, not the PyPI bytes'
    sums: pathlib.Path = signing.write_sha256sums(
        tmp_path, [a, b], {'b.tar.gz': comment}
    )
    assert signing.sha256sums_comments(sums) == {'b.tar.gz': comment}
