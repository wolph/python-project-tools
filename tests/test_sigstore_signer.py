"""Tests for the throttled Sigstore signer script, with sigstore faked."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import os
import pathlib
import subprocess
import sys

import pytest

from python_project_tools import sigstore_signer
from tests.fakes import covering_bundle

START: dt.datetime = dt.datetime(2026, 11, 6, 12, 0, tzinfo=dt.timezone.utc)
CERTIFICATE_LIFE: dt.timedelta = dt.timedelta(minutes=10)


class FakeResponse:
    """The parts of a ``requests.Response`` the signer reads."""

    def __init__(self, status_code: int, retry_after: str = '') -> None:
        self.status_code: int = status_code
        self.headers: dict[str, str] = (
            {'Retry-After': retry_after} if retry_after else {}
        )


class FakeHTTPError(Exception):
    """Stands in for ``requests.HTTPError``."""

    def __init__(self, response: FakeResponse) -> None:
        super().__init__(f'{response.status_code} Client Error')
        self.response: FakeResponse = response


class FakeRekorError(Exception):
    """Stands in for RekorClientError, raised while handling the HTTPError."""


class FakeFulcioError(Exception):
    """Stands in for FulcioClientError, raised from the HTTPError."""


def rekor_error(status: int, retry_after: str = '') -> Exception:
    """A Rekor failure chained the way sigstore 4.5.1 chains it.

    RekorClientError is raised inside the ``except`` of the HTTPError
    without ``from``, so the HTTPError is its ``__context__``.
    """
    error: Exception = FakeRekorError(
        f'Rekor returned an unknown error with HTTP {status}'
    )
    error.__context__ = FakeHTTPError(FakeResponse(status, retry_after))
    return error


def fulcio_error(status: int) -> Exception:
    """FulcioClientError is raised ``from`` the HTTPError, its cause."""
    error: Exception = FakeFulcioError()
    error.__cause__ = FakeHTTPError(FakeResponse(status))
    return error


@dataclasses.dataclass
class World:
    """The faked sigstore side: clock, sleeps, logins and planned failures."""

    clock: dt.datetime = START
    # Seconds the clock moves on per signed file.
    step: float = 1.0
    sleeps: list[float] = dataclasses.field(default_factory=list)
    logins: int = 0
    signers: list[object] = dataclasses.field(default_factory=list)
    signed: list[str] = dataclasses.field(default_factory=list)
    # File name to the errors its next tries raise, in order.
    failures: dict[str, list[Exception]] = dataclasses.field(
        default_factory=dict
    )
    # Signers this many and more raise TokenExpiredError on creation.
    token_lasts_for: int = 1000
    timestamp_fails_for: set[str] = dataclasses.field(default_factory=set)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock += dt.timedelta(seconds=seconds)

    def now(self) -> dt.datetime:
        return self.clock

    def token(self, trust: object) -> str:
        assert trust == 'trust'
        self.logins += 1
        return 'token'

    def signer(self, trust: object, token: object) -> FakeSigner:
        assert (trust, token) == ('trust', 'token')
        if len(self.signers) >= self.token_lasts_for:
            raise sigstore_signer.TokenExpiredError('identity token expired')
        signer: FakeSigner = FakeSigner(self, self.clock + CERTIFICATE_LIFE)
        self.signers.append(signer)
        return signer


class FakeSigner:
    def __init__(self, world: World, expires: dt.datetime) -> None:
        self.world: World = world
        self.expires: dt.datetime = expires

    def sign(self, path: pathlib.Path) -> sigstore_signer.Signed:
        planned: list[Exception] = self.world.failures.get(path.name, [])
        if planned:
            raise planned.pop(0)
        if self.world.clock >= self.expires:
            raise sigstore_signer.CertificateExpiredError(str(path))
        if path.name in self.world.timestamp_fails_for:
            logging.getLogger('sigstore.sign').warning(
                'Unable to use https://timestamp.example to timestamp the '
                'bundle. Failed with 429 Too Many Requests'
            )
        self.world.signed.append(path.name)
        self.world.clock += dt.timedelta(seconds=self.world.step)
        return sigstore_signer.Signed(covering_bundle(path), self.expires)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    faked: World = World()
    monkeypatch.setattr(sigstore_signer, 'sleep', faked.sleep)
    monkeypatch.setattr(sigstore_signer, 'now', faked.now)
    monkeypatch.setattr(sigstore_signer, 'load_trust', lambda: 'trust')
    monkeypatch.setattr(sigstore_signer, 'token_getter', faked.token)
    monkeypatch.setattr(sigstore_signer, 'signer_factory', faked.signer)
    return faked


def make_files(directory: pathlib.Path, count: int) -> list[pathlib.Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths: list[pathlib.Path] = []
    for number in range(count):
        path: pathlib.Path = directory / f'spam-{number}.whl'
        path.write_bytes(f'wheel {number}'.encode())
        paths.append(path)
    return paths


def entries_of(paths: list[pathlib.Path]) -> list[sigstore_signer.Entry]:
    return [
        sigstore_signer.Entry(
            path, path.with_name(path.name + '.sigstore.json')
        )
        for path in paths
    ]


def write_manifest(
    target: pathlib.Path, paths: list[pathlib.Path]
) -> pathlib.Path:
    target.write_text(
        json.dumps(
            [
                {'input': str(e.input), 'bundle': str(e.bundle)}
                for e in entries_of(paths)
            ]
        )
    )
    return target


def run_main(
    tmp_path: pathlib.Path, paths: list[pathlib.Path]
) -> tuple[int, str]:
    """Run the script's main on a manifest of ``paths``, return its status."""
    manifest: pathlib.Path = write_manifest(tmp_path / 'manifest.json', paths)
    status_file: pathlib.Path = tmp_path / 'status.txt'
    code: int = sigstore_signer.main(
        ['--manifest', str(manifest), '--status', str(status_file)]
    )
    return code, status_file.read_text().strip()


def test_every_file_gets_a_covering_bundle_from_one_login(
    world: World, tmp_path: pathlib.Path
) -> None:
    paths: list[pathlib.Path] = make_files(tmp_path / 'v1.0', 3)
    code, message = run_main(tmp_path, paths)
    assert (code, message) == (0, 'all 3 files have a bundle')
    assert world.logins == 1
    assert len(world.signers) == 1
    for entry in entries_of(paths):
        assert sigstore_signer.bundle_covers(entry.input, entry.bundle)
        assert entry.bundle.read_text().endswith('}\n')


def test_existing_bundles_are_skipped_so_a_run_resumes(
    world: World, tmp_path: pathlib.Path
) -> None:
    paths: list[pathlib.Path] = make_files(tmp_path, 3)
    first: sigstore_signer.Entry = entries_of(paths)[0]
    first.bundle.write_text(covering_bundle(first.input))
    before: float = first.bundle.stat().st_mtime
    code, _ = run_main(tmp_path, paths)
    assert code == 0
    assert world.signed == ['spam-1.whl', 'spam-2.whl']
    assert first.bundle.stat().st_mtime == before


def test_nothing_logs_in_when_every_bundle_is_there(
    world: World, tmp_path: pathlib.Path
) -> None:
    paths: list[pathlib.Path] = make_files(tmp_path, 2)
    for entry in entries_of(paths):
        entry.bundle.write_text(covering_bundle(entry.input))
    assert run_main(tmp_path, paths) == (0, 'all 2 files have a bundle')
    assert world.logins == 0


def test_a_foreign_bundle_is_refused_before_the_login(
    world: World, tmp_path: pathlib.Path
) -> None:
    paths: list[pathlib.Path] = make_files(tmp_path, 2)
    foreign: sigstore_signer.Entry = entries_of(paths)[1]
    foreign.bundle.write_text(covering_bundle(paths[0]))
    code, message = run_main(tmp_path, paths)
    assert code == sigstore_signer.EXIT_USAGE
    assert message == (
        f'{foreign.bundle} exists but is not a bundle of {foreign.input}, '
        'refusing to replace it'
    )
    assert world.logins == 0
    assert not entries_of(paths)[0].bundle.exists()


@pytest.mark.parametrize(
    ('manifest', 'message'),
    [
        ('{}', 'manifest is not a list'),
        ('[{"input": "spam.whl", "bundle": "/b"}]', 'manifest input is not'),
        ('[[1]]', 'manifest entry is not an object'),
        ('not json', 'cannot read manifest'),
    ],
)
def test_a_malformed_manifest_exits_2(
    world: World, tmp_path: pathlib.Path, manifest: str, message: str
) -> None:
    path: pathlib.Path = tmp_path / 'manifest.json'
    path.write_text(manifest)
    status: pathlib.Path = tmp_path / 'status.txt'
    code: int = sigstore_signer.main(
        ['--manifest', str(path), '--status', str(status)]
    )
    assert code == sigstore_signer.EXIT_USAGE
    assert message in status.read_text()
    assert world.logins == 0


def test_a_bundle_named_twice_is_refused(
    world: World, tmp_path: pathlib.Path
) -> None:
    paths: list[pathlib.Path] = make_files(tmp_path, 1)
    code, message = run_main(tmp_path, paths * 2)
    assert (code, message) == (2, 'manifest names a bundle more than once')


def test_bundles_are_written_through_a_temporary_file(
    world: World, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    renames: list[tuple[str, str]] = []
    replace = os.replace

    def recording(source: pathlib.Path, target: pathlib.Path) -> None:
        source_path: pathlib.Path = pathlib.Path(source)
        target_path: pathlib.Path = pathlib.Path(target)
        assert not target_path.exists()
        assert source_path.read_text().endswith('}\n')
        renames.append((source_path.name, target_path.name))
        replace(source, target)

    monkeypatch.setattr(sigstore_signer.os, 'replace', recording)
    paths: list[pathlib.Path] = make_files(tmp_path / 'out', 2)
    assert run_main(tmp_path, paths)[0] == 0
    assert renames == [
        ('spam-0.whl.sigstore.json.tmp', 'spam-0.whl.sigstore.json'),
        ('spam-1.whl.sigstore.json.tmp', 'spam-1.whl.sigstore.json'),
    ]
    assert not list((tmp_path / 'out').glob('*.tmp'))


def test_files_are_signed_half_a_second_apart(
    world: World, tmp_path: pathlib.Path
) -> None:
    assert sigstore_signer.PACE_SECONDS == 0.5
    assert run_main(tmp_path, make_files(tmp_path, 4))[0] == 0
    assert world.sleeps == [0.5, 0.5, 0.5]


def test_a_429_backs_off_and_the_next_try_succeeds(
    world: World,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world.failures['spam-0.whl'] = [rekor_error(429), fulcio_error(503)]
    paths: list[pathlib.Path] = make_files(tmp_path, 2)
    assert run_main(tmp_path, paths)[0] == 0
    assert world.sleeps == [5, 10, 0.5]
    assert world.signed == ['spam-0.whl', 'spam-1.whl']
    err: str = capsys.readouterr().err
    assert (
        'ppt: sigstore answered HTTP 429 for spam-0.whl, waiting 5 s before '
        'try 2 of 8'
    ) in err
    assert 'HTTP 503 for spam-0.whl, waiting 10 s before try 3 of 8' in err


def test_retry_after_is_honoured_when_longer_than_the_back_off(
    world: World, tmp_path: pathlib.Path
) -> None:
    world.failures['spam-0.whl'] = [rekor_error(429, retry_after='30')]
    assert run_main(tmp_path, make_files(tmp_path, 1))[0] == 0
    assert world.sleeps == [30]


def test_retry_after_beyond_the_cap_stops_at_once(
    world: World, tmp_path: pathlib.Path
) -> None:
    world.failures['spam-0.whl'] = [rekor_error(429, retry_after='3600')]
    code, message = run_main(tmp_path, make_files(tmp_path, 1))
    assert code == sigstore_signer.EXIT_RATE_LIMITED
    assert message == (
        'rate limited by sigstore after 0 of 1 files, run again later'
    )
    assert world.sleeps == []


def test_eight_throttled_tries_exit_4_and_keep_the_bundles(
    world: World, tmp_path: pathlib.Path
) -> None:
    world.failures['spam-1.whl'] = [rekor_error(429) for _ in range(8)]
    paths: list[pathlib.Path] = make_files(tmp_path, 3)
    code, message = run_main(tmp_path, paths)
    assert code == sigstore_signer.EXIT_RATE_LIMITED
    assert message == (
        'rate limited by sigstore after 1 of 3 files, run again later'
    )
    assert world.sleeps == [0.5, 5, 10, 20, 40, 80, 120, 120]
    assert world.failures['spam-1.whl'] == []
    bundles: list[bool] = [e.bundle.exists() for e in entries_of(paths)]
    assert bundles == [True, False, False]


def test_a_client_error_is_not_retried(
    world: World, tmp_path: pathlib.Path
) -> None:
    world.failures['spam-0.whl'] = [rekor_error(400)]
    code, message = run_main(tmp_path, make_files(tmp_path, 2))
    assert code == sigstore_signer.EXIT_FAILED
    assert message == (
        'sigstore failed after 0 of 2 files: FakeRekorError: Rekor returned '
        'an unknown error with HTTP 400'
    )
    assert world.sleeps == []


def test_the_certificate_is_replaced_from_the_same_token(
    world: World, tmp_path: pathlib.Path
) -> None:
    # Four minutes per file: the second certificate starts at the third file.
    world.step = 240
    assert run_main(tmp_path, make_files(tmp_path, 4))[0] == 0
    assert world.logins == 1
    assert len(world.signers) == 2
    assert world.signed == [f'spam-{n}.whl' for n in range(4)]


def test_a_certificate_that_expired_unannounced_is_replaced(
    world: World, tmp_path: pathlib.Path
) -> None:
    paths: list[pathlib.Path] = make_files(tmp_path, 2)
    world.failures['spam-1.whl'] = [
        sigstore_signer.CertificateExpiredError('spam-1.whl')
    ]
    assert run_main(tmp_path, paths)[0] == 0
    assert len(world.signers) == 2
    assert world.sleeps == [0.5]


def test_an_expired_fresh_certificate_is_not_retried(
    world: World, tmp_path: pathlib.Path
) -> None:
    world.failures['spam-0.whl'] = [
        sigstore_signer.CertificateExpiredError('spam-0.whl')
    ]
    code, _ = run_main(tmp_path, make_files(tmp_path, 1))
    assert code == sigstore_signer.EXIT_FAILED
    assert len(world.signers) == 1


def test_an_expired_token_exits_3_and_keeps_the_bundles(
    world: World, tmp_path: pathlib.Path
) -> None:
    world.step = 300
    world.token_lasts_for = 1
    paths: list[pathlib.Path] = make_files(tmp_path, 4)
    code, message = run_main(tmp_path, paths)
    assert code == sigstore_signer.EXIT_TOKEN_EXPIRED
    assert message == (
        'identity token expired after 2 of 4 files, run again to sign the rest'
    )
    bundles: list[bool] = [e.bundle.exists() for e in entries_of(paths)]
    assert bundles == [True, True, False, False]


def test_a_failing_timestamp_authority_slows_the_run_down(
    world: World, tmp_path: pathlib.Path
) -> None:
    world.timestamp_fails_for = {'spam-0.whl', 'spam-1.whl'}
    paths: list[pathlib.Path] = make_files(tmp_path, 4)
    assert run_main(tmp_path, paths)[0] == 0
    # The bundles are written as the CLI writes them, the pace drops.
    assert all(e.bundle.exists() for e in entries_of(paths))
    assert world.sleeps == [5, 10, 0.5]


def test_progress_is_printed_every_25_files(
    world: World,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths: list[pathlib.Path] = make_files(tmp_path, 60)
    for entry in entries_of(paths)[:5]:
        entry.bundle.write_text(covering_bundle(entry.input))
    assert run_main(tmp_path, paths)[0] == 0
    lines: list[str] = [
        line
        for line in capsys.readouterr().err.splitlines()
        if line.startswith('ppt: signed')
    ]
    assert lines == [
        'ppt: signed 30/60',
        'ppt: signed 55/60',
        'ppt: signed 60/60',
    ]


def test_http_status_follows_cause_and_context() -> None:
    assert sigstore_signer.http_status(rekor_error(429)) == 429
    assert sigstore_signer.http_status(fulcio_error(502)) == 502
    assert sigstore_signer.http_status(ValueError('spam')) is None


@pytest.mark.parametrize(
    ('status', 'expected'),
    [(429, True), (500, True), (503, True), (400, False), (None, False)],
)
def test_retryable_is_429_and_5xx(status: int | None, expected: bool) -> None:
    assert sigstore_signer.retryable(status) is expected


def test_bundle_covers_rejects_foreign_and_broken_bundles(
    tmp_path: pathlib.Path,
) -> None:
    source: pathlib.Path = tmp_path / 'spam.whl'
    other: pathlib.Path = tmp_path / 'eggs.whl'
    source.write_bytes(b'spam')
    other.write_bytes(b'eggs')
    bundle: pathlib.Path = tmp_path / 'spam.whl.sigstore.json'
    bundle.write_text(covering_bundle(source))
    assert sigstore_signer.bundle_covers(source, bundle) is True
    assert sigstore_signer.bundle_covers(other, bundle) is False
    for broken in ('bundle', '[]', '{"messageSignature": 1}'):
        bundle.write_text(broken)
        assert sigstore_signer.bundle_covers(source, bundle) is False
    bundle.unlink()
    assert sigstore_signer.bundle_covers(source, bundle) is False


def test_the_script_runs_without_sigstore_or_the_package(
    tmp_path: pathlib.Path,
) -> None:
    # -I keeps the script directory and the package off sys.path, the way
    # the pinned sigstore environment runs it. Only --help runs, it does
    # not need sigstore, and nothing reaches the network.
    script: str = str(pathlib.Path(sigstore_signer.__file__))
    done: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, '-I', script, '--help'],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert '--manifest' in done.stdout


def test_main_needs_a_manifest(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        sigstore_signer.main([])
    assert raised.value.code == 2
    assert '--manifest' in capsys.readouterr().err
