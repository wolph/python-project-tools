"""Tests for gpg, sigstore and SHA256SUMS handling."""

from __future__ import annotations

import dataclasses
import json
import pathlib
import shutil
import subprocess
import typing

import pytest

from python_project_tools import common, signing
from tests.fakes import completed, covering_bundle

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


@dataclasses.dataclass
class FakeSigner:
    """Stands in for the signer run: records it, writes bundles, exits.

    ``skip`` names files that get no bundle. ``returncode`` and ``message``
    are what the run exits with and leaves in its status file.
    """

    skip: set[str] = dataclasses.field(default_factory=set)
    returncode: int = 0
    message: str = ''
    calls: list[list[str]] = dataclasses.field(default_factory=list)
    manifests: list[list[dict[str, str]]] = dataclasses.field(
        default_factory=list
    )
    timeouts: list[float | None] = dataclasses.field(default_factory=list)

    def __call__(
        self,
        args: list[str],
        check: bool = False,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        capture: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        # The OIDC prompt must reach the terminal.
        assert capture is False
        # The login URL is routed through the relay so it reaches the log.
        assert env == {'BROWSER': signing.relay_browser()}
        self.calls.append(list(args))
        self.timeouts.append(timeout)
        manifest: list[dict[str, str]] = json.loads(
            pathlib.Path(args[args.index('--manifest') + 1]).read_text()
        )
        self.manifests.append(manifest)
        for entry in manifest:
            source: pathlib.Path = pathlib.Path(entry['input'])
            if source.name not in self.skip:
                pathlib.Path(entry['bundle']).write_text(
                    covering_bundle(source)
                )
        if self.message:
            status: pathlib.Path = pathlib.Path(
                args[args.index('--status') + 1]
            )
            status.write_text(self.message + '\n')
        return completed(returncode=self.returncode)


def wheels(*relative: str, root: pathlib.Path) -> list[pathlib.Path]:
    paths: list[pathlib.Path] = []
    for name in relative:
        path: pathlib.Path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
        paths.append(path)
    return paths


def test_sigstore_sign_runs_the_signer_once_with_a_manifest(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake: FakeSigner = FakeSigner()
    monkeypatch.setattr(common, 'run', fake)
    paths: list[pathlib.Path] = wheels(
        'v1.0/a.whl', 'v2.0/a.whl', 'v2.0/b.tar.gz', root=tmp_path
    )
    out: list[pathlib.Path] = signing.sigstore_sign(paths)
    assert signing.SIGSTORE_ARGV == [
        'uvx',
        '--from',
        'sigstore==4.5.1',
        'python',
        '-I',
    ]
    assert len(fake.calls) == 1
    argv: list[str] = fake.calls[0]
    assert argv[:6] == [*signing.SIGSTORE_ARGV, str(signing.SIGNER_SCRIPT)]
    assert signing.SIGNER_SCRIPT.name == 'sigstore_signer.py'
    assert signing.SIGNER_SCRIPT.is_file()
    assert argv[6] == '--manifest'
    assert argv[8] == '--status'
    assert fake.manifests == [
        [
            {
                'input': str(path),
                'bundle': str(path.with_name(path.name + '.sigstore.json')),
            }
            for path in paths
        ]
    ]
    # Login time plus three seconds per file.
    assert fake.timeouts == [900 + 3 * 3]
    assert out == [signing.bundle_path(path) for path in paths]
    assert all(signing.bundle_covers(path) for path in paths)
    # The manifest lived in a temporary directory that is gone.
    assert not pathlib.Path(argv[7]).exists()


def test_sigstore_sign_lists_a_repeated_file_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake: FakeSigner = FakeSigner()
    monkeypatch.setattr(common, 'run', fake)
    (wheel,) = wheels('a.whl', root=tmp_path)
    out: list[pathlib.Path] = signing.sigstore_sign([wheel, wheel])
    assert [entry['input'] for entry in fake.manifests[0]] == [str(wheel)]
    assert fake.timeouts == [903]
    assert out == [signing.bundle_path(wheel)] * 2


def test_sigstore_sign_with_no_files_runs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake: FakeSigner = FakeSigner()
    monkeypatch.setattr(common, 'run', fake)
    assert signing.sigstore_sign([]) == []
    assert fake.calls == []


def test_sigstore_sign_raises_when_bundle_missing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(common, 'run', FakeSigner(skip={'a.tar.gz'}))
    paths: list[pathlib.Path] = wheels('a.whl', 'a.tar.gz', root=tmp_path)
    with pytest.raises(signing.SigningError, match=r'a\.tar\.gz\.sigstore'):
        signing.sigstore_sign(paths)


@pytest.mark.parametrize(
    ('returncode', 'message'),
    [
        (
            3,
            'identity token expired after 1 of 2 files, run again to sign '
            'the rest',
        ),
        (4, 'rate limited by sigstore after 1 of 2 files, run again later'),
    ],
)
def test_an_expired_token_or_a_rate_limit_is_a_runtime_error(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    message: str,
) -> None:
    fake: FakeSigner = FakeSigner(
        skip={'b.whl'}, returncode=returncode, message=message
    )
    monkeypatch.setattr(common, 'run', fake)
    paths: list[pathlib.Path] = wheels('a.whl', 'b.whl', root=tmp_path)
    with pytest.raises(RuntimeError) as raised:
        signing.sigstore_sign(paths)
    assert not isinstance(raised.value, signing.SigningError)
    assert str(raised.value) == message
    # The bundle written before the stop stays for the caller to use.
    assert signing.bundle_covers(paths[0]) is True
    assert signing.bundle_covers(paths[1]) is False


def test_sigstore_sign_raises_on_nonzero_exit(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake: FakeSigner = FakeSigner(
        returncode=1, message='sigstore failed after 0 of 1 files: spam'
    )
    monkeypatch.setattr(common, 'run', fake)
    with pytest.raises(signing.SigningError) as raised:
        signing.sigstore_sign(wheels('a.whl', root=tmp_path))
    assert str(raised.value) == (
        'sigstore failed with exit status 1: sigstore failed after 0 of 1 '
        'files: spam'
    )


def test_a_signer_that_leaves_no_message_points_at_its_output(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(common, 'run', FakeSigner(returncode=2))
    with pytest.raises(signing.SigningError) as raised:
        signing.sigstore_sign(wheels('a.whl', root=tmp_path))
    assert str(raised.value) == (
        'sigstore failed with exit status 2: see its output above'
    )


def test_bundle_covers_checks_the_bundle_next_to_the_file(
    tmp_path: pathlib.Path,
) -> None:
    (wheel,) = wheels('a.whl', root=tmp_path)
    assert signing.bundle_covers(wheel) is False
    signing.bundle_path(wheel).write_text('bundle')
    assert signing.bundle_covers(wheel) is False
    signing.bundle_path(wheel).write_text(covering_bundle(wheel))
    assert signing.bundle_covers(wheel) is True


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
        signing.sigstore_sign(wheels('a.whl', 'b.whl', root=tmp_path))
    assert str(raised.value) == (
        'sigstore did not finish within 906 s, the browser login never '
        'completed or signing ran out of time'
    )
    assert seen == [906]
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
