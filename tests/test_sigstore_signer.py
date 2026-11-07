"""Tests for the throttled Sigstore signer script, with sigstore faked."""

from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import json
import logging
import os
import pathlib
import subprocess
import sys
import threading
import time
import typing

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
    """The faked sigstore side: clock, sleeps, logins and planned failures.

    Workers call it from several threads, so every change holds ``lock``.
    """

    clock: dt.datetime = START
    # Seconds the clock moves on per signed file.
    step: float = 1.0
    sleeps: list[float] = dataclasses.field(default_factory=list)
    logins: int = 0
    signers: list[object] = dataclasses.field(default_factory=list)
    signed: list[str] = dataclasses.field(default_factory=list)
    # Thread name of each signed file, in the order of ``signed``.
    threads: list[str] = dataclasses.field(default_factory=list)
    # File name to the errors its next tries raise, in order.
    failures: dict[str, list[Exception]] = dataclasses.field(
        default_factory=dict
    )
    # Files the token of each login signs before it expires, by login.
    # A login past the end of the list gets a token that never expires.
    token_signs: list[int] = dataclasses.field(default_factory=list)
    uses: collections.Counter[str] = dataclasses.field(
        default_factory=collections.Counter
    )
    # A token this old gets no new certificate, as Fulcio refuses it.
    token_life: dt.timedelta | None = None
    issued: dict[str, dt.datetime] = dataclasses.field(default_factory=dict)
    # Files whose try raised TokenExpiredError, in order.
    expired_tries: list[str] = dataclasses.field(default_factory=list)
    timestamp_fails_for: set[str] = dataclasses.field(default_factory=set)
    # Called with the login number before the token is handed out.
    on_login: typing.Callable[[int], None] | None = None
    # Called with the file name at the start of every sign call.
    on_sign: typing.Callable[[str], None] | None = None
    # Called with the token before a try raises TokenExpiredError.
    on_expired: typing.Callable[[str], None] | None = None
    lock: threading.RLock = dataclasses.field(default_factory=threading.RLock)

    def sleep(self, seconds: float) -> None:
        with self.lock:
            self.sleeps.append(seconds)
            self.clock += dt.timedelta(seconds=seconds)

    def now(self) -> dt.datetime:
        with self.lock:
            return self.clock

    def token(self, trust: object) -> str:
        assert trust == 'trust'
        with self.lock:
            self.logins += 1
            number: int = self.logins
        if self.on_login is not None:
            self.on_login(number)
        with self.lock:
            self.issued[f'token-{number}'] = self.clock
        return f'token-{number}'

    def limit(self, token: str) -> int | None:
        number: int = int(token.removeprefix('token-'))
        if number > len(self.token_signs):
            return None
        return self.token_signs[number - 1]

    def signer(self, trust: object, token: str) -> FakeSigner:
        assert trust == 'trust'
        assert token.startswith('token-')
        if (
            self.token_life is not None
            and sigstore_signer.now() >= self.issued[token] + self.token_life
        ):
            raise sigstore_signer.TokenExpiredError('identity token expired')
        signer: FakeSigner = FakeSigner(
            self, token, sigstore_signer.now() + CERTIFICATE_LIFE
        )
        with self.lock:
            self.signers.append(signer)
        return signer


class FakeSigner:
    def __init__(self, world: World, token: str, expires: dt.datetime) -> None:
        self.world: World = world
        self.token: str = token
        self.expires: dt.datetime = expires

    def _planned(self, name: str) -> Exception | None:
        with self.world.lock:
            planned: list[Exception] = self.world.failures.get(name, [])
            return planned.pop(0) if planned else None

    def _token_expired(self) -> bool:
        limit: int | None = self.world.limit(self.token)
        with self.world.lock:
            return limit is not None and self.world.uses[self.token] >= limit

    def _expire(self, name: str) -> typing.NoReturn:
        with self.world.lock:
            self.world.expired_tries.append(name)
        if self.world.on_expired is not None:
            self.world.on_expired(self.token)
        raise sigstore_signer.TokenExpiredError(name)

    def sign(self, path: pathlib.Path) -> sigstore_signer.Signed:
        if self.world.on_sign is not None:
            self.world.on_sign(path.name)
        planned: Exception | None = self._planned(path.name)
        if planned is not None:
            raise planned
        if self._token_expired():
            self._expire(path.name)
        if sigstore_signer.now() >= self.expires:
            raise sigstore_signer.CertificateExpiredError(str(path))
        if path.name in self.world.timestamp_fails_for:
            logging.getLogger('sigstore.sign').warning(
                'Unable to use https://timestamp.example to timestamp the '
                'bundle. Failed with 429 Too Many Requests'
            )
        with self.world.lock:
            self.world.uses[self.token] += 1
            self.world.signed.append(path.name)
            self.world.threads.append(threading.current_thread().name)
            self.world.clock += dt.timedelta(seconds=self.world.step)
        return sigstore_signer.Signed(covering_bundle(path), self.expires)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    """The faked sigstore side, with one worker unless a test asks for more.

    One worker keeps the order of files and sleeps exact, which the tests
    of pacing and back-off compare against.
    """
    faked: World = World()
    monkeypatch.setattr(sigstore_signer, 'sleep', faked.sleep)
    monkeypatch.setattr(sigstore_signer, 'now', faked.now)
    monkeypatch.setattr(sigstore_signer, 'load_trust', lambda: 'trust')
    monkeypatch.setattr(sigstore_signer, 'token_getter', faked.token)
    monkeypatch.setattr(sigstore_signer, 'signer_factory', faked.signer)
    monkeypatch.setattr(sigstore_signer, 'WORKERS', 1)
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


def relogin_lines(err: str) -> list[str]:
    return [
        line
        for line in err.splitlines()
        if line.startswith('ppt: Sigstore login needed again')
    ]


def test_a_token_too_old_for_a_new_certificate_logs_in_again(
    world: World,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Five minutes per file and a nine minute token: the certificate is
    # due for renewal at the third file, when the token is past its end.
    world.step = 300
    world.token_life = dt.timedelta(minutes=9)
    paths: list[pathlib.Path] = make_files(tmp_path, 4)
    assert run_main(tmp_path, paths) == (0, 'all 4 files have a bundle')
    assert world.logins == 2
    assert world.signed == [f'spam-{n}.whl' for n in range(4)]
    assert all(e.bundle.exists() for e in entries_of(paths))
    assert relogin_lines(capsys.readouterr().err) == [
        'ppt: Sigstore login needed again, 2 files left',
    ]


def test_an_expired_token_logs_in_again_and_the_run_completes(
    world: World,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world.token_signs = [2, 2, 2]
    paths: list[pathlib.Path] = make_files(tmp_path, 6)
    assert run_main(tmp_path, paths) == (0, 'all 6 files have a bundle')
    assert world.logins == 3
    assert len(world.signers) == 3
    assert world.signed == [f'spam-{n}.whl' for n in range(6)]
    assert world.expired_tries == ['spam-2.whl', 'spam-4.whl']
    assert all(e.bundle.exists() for e in entries_of(paths))
    assert relogin_lines(capsys.readouterr().err) == [
        'ppt: Sigstore login needed again, 4 files left',
        'ppt: Sigstore login needed again, 2 files left',
    ]


def test_out_of_logins_exits_3_and_keeps_the_bundles(
    world: World,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert sigstore_signer.MAX_LOGINS == 6
    monkeypatch.setattr(sigstore_signer, 'MAX_LOGINS', 2)
    world.token_signs = [1, 1, 1]
    paths: list[pathlib.Path] = make_files(tmp_path, 4)
    code, message = run_main(tmp_path, paths)
    assert code == sigstore_signer.EXIT_TOKEN_EXPIRED
    assert message == (
        'identity token expired after 2 of 4 files, run again to sign the rest'
    )
    assert world.logins == 2
    bundles: list[bool] = [e.bundle.exists() for e in entries_of(paths)]
    assert bundles == [True, True, False, False]
    assert relogin_lines(capsys.readouterr().err) == [
        'ppt: Sigstore login needed again, 3 files left',
    ]


def test_a_login_that_never_returns_exits_5(
    world: World, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sigstore_signer.LOGIN_WAIT_SECONDS == 900
    monkeypatch.setattr(sigstore_signer, 'LOGIN_WAIT_SECONDS', 0.2)
    released: threading.Event = threading.Event()

    def hang_on_second(number: int) -> None:
        if number == 2:
            released.wait(10)

    world.on_login = hang_on_second
    world.token_signs = [2]
    paths: list[pathlib.Path] = make_files(tmp_path, 4)
    try:
        code, message = run_main(tmp_path, paths)
    finally:
        released.set()
    assert code == sigstore_signer.EXIT_NO_LOGIN == 5
    assert message == (
        'no login within 0.2 s after 2 of 4 files, run again to sign the rest'
    )
    bundles: list[bool] = [e.bundle.exists() for e in entries_of(paths)]
    assert bundles == [True, True, False, False]


def test_a_first_login_that_never_returns_exits_5(
    world: World, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sigstore_signer, 'LOGIN_WAIT_SECONDS', 0.1)
    released: threading.Event = threading.Event()

    def hang(_number: int) -> None:
        released.wait(10)

    world.on_login = hang
    try:
        code, message = run_main(tmp_path, make_files(tmp_path, 2))
    finally:
        released.set()
    assert code == sigstore_signer.EXIT_NO_LOGIN
    assert message == (
        'no login within 0.1 s after 0 of 2 files, run again to sign the rest'
    )
    assert world.signed == []


class FakeIdentityError(Exception):
    """Stands in for sigstore's IdentityError of a refused login."""


def test_a_refused_relogin_exits_1_and_keeps_the_bundles(
    world: World, tmp_path: pathlib.Path
) -> None:
    def refuse_second(number: int) -> None:
        if number == 2:
            raise FakeIdentityError('access_denied')

    world.on_login = refuse_second
    world.token_signs = [1]
    paths: list[pathlib.Path] = make_files(tmp_path, 3)
    code, message = run_main(tmp_path, paths)
    assert code == sigstore_signer.EXIT_FAILED
    assert message == (
        'sigstore failed after 1 of 3 files: FakeIdentityError: access_denied'
    )
    assert world.logins == 2
    bundles: list[bool] = [e.bundle.exists() for e in entries_of(paths)]
    assert bundles == [True, False, False]


def test_three_workers_give_every_entry_exactly_one_bundle(
    world: World,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert sigstore_signer.WORKERS == 1
    monkeypatch.setattr(sigstore_signer, 'WORKERS', 3)
    # The first three calls meet, so three threads are signing at once.
    meeting: threading.Barrier = threading.Barrier(3, timeout=5)
    calls: list[str] = []

    def meet_first_three(name: str) -> None:
        with world.lock:
            calls.append(name)
            first: bool = len(calls) <= 3
        if first:
            meeting.wait()

    world.on_sign = meet_first_three
    written: collections.Counter[str] = collections.Counter()
    replace = os.replace

    def counting(source: pathlib.Path, target: pathlib.Path) -> None:
        with world.lock:
            written[pathlib.Path(target).name] += 1
        replace(source, target)

    monkeypatch.setattr(sigstore_signer.os, 'replace', counting)
    paths: list[pathlib.Path] = make_files(tmp_path, 60)
    assert run_main(tmp_path, paths) == (0, 'all 60 files have a bundle')
    names: list[str] = [path.name for path in paths]
    assert sorted(world.signed) == sorted(names)
    assert written == {f'{name}.sigstore.json': 1 for name in names}
    assert set(world.threads) == {f'ppt-sigstore-{n}' for n in (1, 2, 3)}
    assert world.logins == 1
    assert len(world.signers) == 1
    for entry in entries_of(paths):
        assert sigstore_signer.bundle_covers(entry.input, entry.bundle)
    progress: list[str] = [
        line
        for line in capsys.readouterr().err.splitlines()
        if line.startswith('ppt: signed')
    ]
    assert progress == [
        'ppt: signed 25/60',
        'ppt: signed 50/60',
        'ppt: signed 60/60',
    ]


def test_a_429_on_one_worker_pauses_the_others(
    world: World, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real time: the pause is about threads waiting on each other.
    monkeypatch.setattr(sigstore_signer, 'sleep', time.sleep)
    monkeypatch.setattr(sigstore_signer, 'now', sigstore_signer._utc_now)
    monkeypatch.setattr(sigstore_signer, 'WORKERS', 3)
    monkeypatch.setattr(sigstore_signer, 'PACE_SECONDS', 0.01)
    monkeypatch.setattr(sigstore_signer, 'BACKOFF_START_SECONDS', 0.4)
    meeting: threading.Barrier = threading.Barrier(3, timeout=5)
    starts: list[tuple[float, str, str]] = []
    hit: list[float] = []

    def throttle_spam_0(name: str) -> None:
        thread: str = threading.current_thread().name
        with world.lock:
            starts.append((time.monotonic(), name, thread))
            first_round: bool = len(starts) <= 3
        if not first_round:
            return
        # spam-0, spam-1 and spam-2 are in flight together, then spam-0
        # is throttled while the other two finish a little later.
        meeting.wait()
        if name == 'spam-0.whl':
            hit.append(time.monotonic())
            raise rekor_error(429)
        time.sleep(0.1)

    world.on_sign = throttle_spam_0
    paths: list[pathlib.Path] = make_files(tmp_path, 9)
    assert run_main(tmp_path, paths)[0] == 0
    assert all(e.bundle.exists() for e in entries_of(paths))
    assert sorted(name for _, name, _ in starts[:3]) == [
        'spam-0.whl',
        'spam-1.whl',
        'spam-2.whl',
    ]
    later: list[tuple[float, str, str]] = starts[3:]
    assert len(later) == 7
    # Nobody tried again before the back-off of spam-0 was over, the
    # workers of spam-1 and spam-2 included. 10 ms of slack between the
    # wall clock of the signer and the monotonic clock of the test.
    assert min(start for start, _, _ in later) >= hit[0] + 0.4 - 0.01
    others: set[str] = {
        thread for _, name, thread in starts[:3] if name != 'spam-0.whl'
    }
    assert len(others) == 2
    assert others <= {thread for _, _, thread in later}


def test_a_token_expiry_mid_batch_puts_the_in_flight_files_back(
    world: World,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sigstore_signer, 'WORKERS', 3)
    world.token_signs = [3]
    # The three tries after the first token ran out are in flight at once.
    meeting: threading.Barrier = threading.Barrier(3, timeout=5)

    def meet_on_first_token(token: str) -> None:
        if token == 'token-1':
            meeting.wait()

    world.on_expired = meet_on_first_token
    paths: list[pathlib.Path] = make_files(tmp_path, 12)
    assert run_main(tmp_path, paths) == (0, 'all 12 files have a bundle')
    assert world.logins == 2
    assert len(world.expired_tries) == 3
    assert len(set(world.expired_tries)) == 3
    counts: collections.Counter[str] = collections.Counter(world.signed)
    assert counts == {path.name: 1 for path in paths}
    assert set(world.expired_tries) <= set(world.signed)
    assert all(e.bundle.exists() for e in entries_of(paths))
    assert relogin_lines(capsys.readouterr().err) == [
        'ppt: Sigstore login needed again, 9 files left',
    ]


def test_leave_skips_the_shutdown_only_after_a_missing_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exits: list[int] = []

    def fake_exit(status: int) -> typing.NoReturn:
        exits.append(status)
        raise SystemExit(status)

    monkeypatch.setattr(sigstore_signer.os, '_exit', fake_exit)
    with pytest.raises(SystemExit):
        sigstore_signer.leave(sigstore_signer.EXIT_NO_LOGIN)
    assert exits == [5]
    with pytest.raises(SystemExit) as raised:
        sigstore_signer.leave(sigstore_signer.EXIT_TOKEN_EXPIRED)
    assert raised.value.code == 3
    assert exits == [5]


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
