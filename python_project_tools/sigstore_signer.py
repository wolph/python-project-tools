"""Sign files with Sigstore a few at a time, logging in again when needed.

This script runs inside the pinned sigstore environment, not inside
python_project_tools::

    uvx --from sigstore==4.5.1 python -I sigstore_signer.py --manifest FILE

so it imports the standard library only at import time, and sigstore only
inside the functions that talk to it. ``signing`` imports this module for
its path and its exit codes, which is why nothing here runs on import.

The manifest is a JSON list of ``{"input": PATH, "bundle": PATH}`` with
absolute paths. sigstore's own ``sign`` command signs every file in
parallel, and on 1247 files the public instance answered with HTTP 429
until the command gave up without writing a single bundle. This script is
the slow and resumable version of that command:

* An identity token from the same browser OIDC flow the command uses, so
  BROWSER and the login relay still apply. The token lives about ten
  minutes, so when it expires the run asks for a new login and goes on,
  MAX_LOGINS logins per run at most. Each login waits LOGIN_WAIT_SECONDS
  for the browser and stops the run with EXIT_NO_LOGIN after that. A run
  out of logins stops with EXIT_TOKEN_EXPIRED.
* One signing certificate, reused while it is valid and replaced from the
  same token shortly before it expires.
* WORKERS files at a time, sharing that certificate, each worker
  PACE_SECONDS between its files. HTTP 429 and 5xx answers back off
  exponentially and pause every worker, and a file that still fails after
  ATTEMPTS tries stops the run with EXIT_RATE_LIMITED.
* Each bundle is written as soon as its file is signed, through a
  temporary file and a rename, so an interrupted run keeps what it signed.
  A bundle that already exists and covers its file is skipped, which makes
  a second run continue where the first one stopped.
"""

from __future__ import annotations

import argparse
import base64
import collections
import collections.abc
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import pathlib
import sys
import threading
import time
import typing

PACE_SECONDS: float = 0.5
BACKOFF_START_SECONDS: float = 5
BACKOFF_MAX_SECONDS: float = 120
# Tries per file, the first one included.
ATTEMPTS: int = 8
PROGRESS_EVERY: int = 25
# Files signed at the same time. sigstore's Signer is shared by the threads
# of ``sigstore sign`` as well: its certificate and key are fixed once it
# exists, and the Rekor and timestamp clients keep a session per thread.
WORKERS: int = 3
# Browser logins per run, the first one included.
MAX_LOGINS: int = 6
# How long one login waits for the browser callback. sigstore's own flow
# waits forever.
LOGIN_WAIT_SECONDS: float = 900
# A certificate this close to its end is replaced before the next file,
# so no entry reaches Rekor after the certificate expired.
CERTIFICATE_MARGIN: dt.timedelta = dt.timedelta(seconds=60)
# sigstore logs a failed timestamp request here and signs on without one.
TIMESTAMP_LOGGER: str = 'sigstore.sign'
HASH_CHUNK: int = 1 << 20

EXIT_OK: int = 0
EXIT_FAILED: int = 1
EXIT_USAGE: int = 2
EXIT_TOKEN_EXPIRED: int = 3
EXIT_RATE_LIMITED: int = 4
EXIT_NO_LOGIN: int = 5

DONE_MESSAGE: str = 'all {total} files have a bundle'
TOKEN_EXPIRED_MESSAGE: str = (
    'identity token expired after {done} of {total} files, run again to '
    'sign the rest'
)
RATE_LIMITED_MESSAGE: str = (
    'rate limited by sigstore after {done} of {total} files, run again later'
)
NO_LOGIN_MESSAGE: str = (
    'no login within {seconds:g} s after {done} of {total} files, run again '
    'to sign the rest'
)
FAILED_MESSAGE: str = 'sigstore failed after {done} of {total} files: {error}'
PROGRESS_MESSAGE: str = 'ppt: signed {done}/{total}'
RELOGIN_MESSAGE: str = (
    'ppt: Sigstore login needed again, {remaining} files left'
)
RETRY_MESSAGE: str = (
    'ppt: sigstore answered HTTP {status} for {name}, waiting {seconds:g} s '
    'before try {attempt} of {attempts}'
)
TIMESTAMP_MESSAGE: str = (
    'ppt: the timestamp authority failed for {name}, the bundle relies on '
    'the Rekor entry time, waiting {seconds:g} s'
)


class ManifestError(Exception):
    """The manifest is unreadable, or an existing bundle is not its file's."""


class TokenExpiredError(Exception):
    """The identity token can no longer get a signing certificate."""


class CertificateExpiredError(Exception):
    """The cached signing certificate expired before the file was signed."""


class RateLimitedError(Exception):
    """sigstore kept answering 429 or 5xx for one file."""


class NoLoginError(Exception):
    """No browser login arrived within LOGIN_WAIT_SECONDS."""


class StoppedError(Exception):
    """Another worker stopped the run, this one leaves without signing."""


@dataclasses.dataclass(frozen=True)
class Entry:
    """One file to sign and the bundle it gets."""

    input: pathlib.Path
    bundle: pathlib.Path


@dataclasses.dataclass(frozen=True)
class Signed:
    """A bundle as JSON text and the end of its certificate's validity."""

    bundle_json: str
    expires: dt.datetime


class FileSigner(typing.Protocol):
    """Signs one file per call with one cached certificate."""

    def sign(self, path: pathlib.Path) -> Signed:
        """The bundle of ``path``, or CertificateExpiredError."""
        ...


def _production_trust() -> typing.Any:
    """The trust configuration of the public instance, as the CLI loads it."""
    from sigstore.models import (  # ty: ignore[import-not-found]
        ClientTrustConfig,
    )

    return ClientTrustConfig.production()


def _browser_token(trust: typing.Any) -> typing.Any:
    """An identity token from the interactive browser flow.

    The same issuer and client as ``sigstore sign`` without ambient
    credentials, so the token names the person at the browser.
    """
    from sigstore.oidc import Issuer  # ty: ignore[import-not-found]

    issuer: typing.Any = Issuer(trust.signing_config.get_oidc_url())
    return issuer.identity_token()


class _LibrarySigner:
    """sigstore's ``Signer`` behind the FileSigner protocol."""

    def __init__(
        self,
        signer: typing.Any,
        expired_certificate: type[Exception],
        expired_identity: type[Exception],
    ) -> None:
        self._signer: typing.Any = signer
        self._expired_certificate: type[Exception] = expired_certificate
        self._expired_identity: type[Exception] = expired_identity

    def sign(self, path: pathlib.Path) -> Signed:
        """Sign the bytes of ``path`` as a hashedrekord, like the CLI.

        Both expiry errors are raised before sigstore makes a Rekor entry,
        so a file that raised one can be signed again without a duplicate.
        """
        try:
            bundle: typing.Any = self._signer.sign_artifact(path.read_bytes())
        except self._expired_certificate as error:
            raise CertificateExpiredError(str(path)) from error
        except self._expired_identity as error:
            raise TokenExpiredError(str(path)) from error
        expires: dt.datetime = bundle.signing_certificate.not_valid_after_utc
        return Signed(bundle.to_json(), expires)


def _library_signer(trust: typing.Any, token: typing.Any) -> FileSigner:
    """A signer with a fresh certificate from Fulcio for ``token``.

    Raises TokenExpiredError when the token is past its validity, which is
    the moment a run can go no further without a new login.
    """
    from sigstore._internal.fulcio import (  # ty: ignore[import-not-found]
        ExpiredCertificate,
    )
    from sigstore.oidc import ExpiredIdentity  # ty: ignore[import-not-found]
    from sigstore.sign import (  # ty: ignore[import-not-found]
        Signer,
        SigningContext,
    )

    if not token.in_validity_period():
        raise TokenExpiredError('identity token expired')
    context: typing.Any = SigningContext.from_trust_config(trust)
    try:
        signer: typing.Any = Signer(token, context, cache=True)
    except ExpiredIdentity as error:
        raise TokenExpiredError('identity token expired') from error
    return _LibrarySigner(signer, ExpiredCertificate, ExpiredIdentity)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# Module attributes so tests can replace the clock and the sigstore side.
sleep: collections.abc.Callable[[float], None] = time.sleep
now: collections.abc.Callable[[], dt.datetime] = _utc_now
load_trust: collections.abc.Callable[[], typing.Any] = _production_trust
token_getter: collections.abc.Callable[[typing.Any], typing.Any] = (
    _browser_token
)
signer_factory: collections.abc.Callable[
    [typing.Any, typing.Any], FileSigner
] = _library_signer

# One line at a time, also when several workers report at once.
_SAY_LOCK: threading.Lock = threading.Lock()


def _say(message: str) -> None:
    with _SAY_LOCK:
        sys.stderr.write(message + '\n')
        sys.stderr.flush()


def _sha256(path: pathlib.Path) -> bytes:
    digest: typing.Any = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK), b''):
            digest.update(chunk)
    return digest.digest()


def bundle_covers(source: pathlib.Path, bundle: pathlib.Path) -> bool:
    """True when ``bundle`` is a message signature over the bytes of source.

    Only the digest is compared, the signature is not verified. That is
    enough to tell a bundle of this file from a stale or foreign one.
    """
    try:
        loaded: typing.Any = json.loads(bundle.read_text(encoding='utf-8'))
        digest: str = loaded['messageSignature']['messageDigest']['digest']
        return base64.b64decode(digest) == _sha256(source)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _entry(item: object) -> Entry:
    if not isinstance(item, dict):
        raise ManifestError(f'manifest entry is not an object: {item!r}')
    paths: list[pathlib.Path] = []
    for key in ('input', 'bundle'):
        value: object = item.get(key)
        if not isinstance(value, str) or not os.path.isabs(value):
            raise ManifestError(f'manifest {key} is not an absolute path')
        paths.append(pathlib.Path(value))
    if not paths[0].is_file():
        raise ManifestError(f'input is not a file: {paths[0]}')
    return Entry(paths[0], paths[1])


def read_manifest(path: pathlib.Path) -> list[Entry]:
    """The entries of a manifest, refusing anything malformed or repeated."""
    try:
        loaded: object = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise ManifestError(f'cannot read manifest {path}: {error}') from error
    if not isinstance(loaded, list):
        raise ManifestError('manifest is not a list')
    entries: list[Entry] = [_entry(item) for item in loaded]
    bundles: list[pathlib.Path] = [entry.bundle for entry in entries]
    if len(set(bundles)) != len(bundles):
        raise ManifestError('manifest names a bundle more than once')
    return entries


def pending(entries: list[Entry]) -> list[Entry]:
    """The entries without a bundle yet.

    A bundle that exists but does not cover its file is refused rather
    than signed over: something other than this script wrote it.
    """
    todo: list[Entry] = []
    for entry in entries:
        if not entry.bundle.exists():
            todo.append(entry)
        elif not bundle_covers(entry.input, entry.bundle):
            raise ManifestError(
                f'{entry.bundle} exists but is not a bundle of '
                f'{entry.input}, refusing to replace it'
            )
    return todo


def write_bundle(target: pathlib.Path, text: str) -> None:
    """Write ``text`` to ``target`` through ``<target>.tmp`` and a rename."""
    temporary: pathlib.Path = target.with_name(target.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        handle.write(text + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def http_status(error: BaseException) -> int | None:
    """The HTTP status of the first response found in the exception chain.

    sigstore wraps the ``requests.HTTPError`` of Rekor and Fulcio in its
    own exceptions, as ``__context__`` and ``__cause__`` respectively.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response: object = getattr(current, 'response', None)
        status: object = getattr(response, 'status_code', None)
        if isinstance(status, int):
            return status
        current = current.__cause__ or current.__context__
    return None


def _retry_after(error: BaseException) -> float | None:
    """The Retry-After seconds of the response in the chain, if any."""
    current: BaseException | None = error
    for _ in range(8):
        if current is None:
            break
        headers: object = getattr(
            getattr(current, 'response', None), 'headers', None
        )
        value: object = getattr(headers, 'get', lambda _key: None)(
            'Retry-After'
        )
        if isinstance(value, str) and value.strip().isdigit():
            return float(value.strip())
        current = current.__cause__ or current.__context__
    return None


def retryable(status: int | None) -> bool:
    """Throttled (429) or a server error (5xx), worth trying again later."""
    return status is not None and (status == 429 or 500 <= status <= 599)


def login(trust: typing.Any) -> typing.Any:
    """A token from ``token_getter``, waiting LOGIN_WAIT_SECONDS at most.

    sigstore's browser flow polls for the callback without an end, so it
    runs in a daemon thread that is abandoned when no login arrives. A
    login that fails raises its own error.
    """
    tokens: list[typing.Any] = []
    errors: list[Exception] = []

    def attempt() -> None:
        try:
            tokens.append(token_getter(trust))
        except Exception as error:
            errors.append(error)

    thread: threading.Thread = threading.Thread(
        target=attempt, name='ppt-sigstore-login', daemon=True
    )
    thread.start()
    thread.join(LOGIN_WAIT_SECONDS)
    if thread.is_alive():
        raise NoLoginError(f'no login within {LOGIN_WAIT_SECONDS:g} s')
    if errors:
        raise errors[0]
    if not tokens:
        raise NoLoginError('the login ended without an identity token')
    return tokens[0]


@dataclasses.dataclass(frozen=True)
class Lease:
    """The signer a worker signs one file with, and where it came from."""

    signer: FileSigner
    # No bundle of this signer has revealed its certificate yet.
    fresh: bool


@dataclasses.dataclass
class Session:
    """The token of the run and the signer whose certificate is in use.

    Every worker shares it. ``generation`` counts the logins after the
    first, so a worker that saw the token expire can tell whether another
    worker already logged in again.
    """

    trust: typing.Any
    token: typing.Any
    signer: FileSigner | None = None
    # None until the first bundle of this signer reveals its certificate.
    expires: dt.datetime | None = None
    generation: int = 0
    logins: int = 1
    logging_in: bool = False
    closed: bool = False
    changed: threading.Condition = dataclasses.field(
        default_factory=threading.Condition
    )

    def _settle(self) -> None:
        """Wait, holding ``changed``, until no login is running."""
        self.changed.wait_for(lambda: self.closed or not self.logging_in)
        if self.closed:
            raise StoppedError

    def lease(self) -> Lease:
        """The signer, replaced first when its certificate is nearly over."""
        with self.changed:
            self._settle()
            stale: bool = (
                self.expires is not None
                and now() + CERTIFICATE_MARGIN >= self.expires
            )
            if self.signer is None or stale:
                self.signer = None
                self.expires = None
                self.signer = signer_factory(self.trust, self.token)
            return Lease(self.signer, self.expires is None)

    def _discard(self, signer: FileSigner) -> None:
        """Drop ``signer`` unless another worker replaced it already."""
        with self.changed:
            if self.signer is signer:
                self.signer = None
                self.expires = None

    def _signed_by(self, signer: FileSigner, signed: Signed) -> Signed:
        with self.changed:
            if self.signer is signer:
                self.expires = signed.expires
        return signed

    def sign(self, path: pathlib.Path) -> Signed:
        """Sign one file, replacing a certificate that expired once.

        A certificate that is already expired on a fresh signer means the
        clock or the service is wrong, so that error is passed on.
        """
        lease: Lease = self.lease()
        try:
            return self._signed_by(lease.signer, lease.signer.sign(path))
        except CertificateExpiredError:
            if lease.fresh:
                raise
            self._discard(lease.signer)
        lease = self.lease()
        return self._signed_by(lease.signer, lease.signer.sign(path))

    def begin_login(self, seen: int) -> bool:
        """True when the caller is to log in again for generation ``seen``.

        False when another worker logged in since. Raises TokenExpiredError
        when the run used up its MAX_LOGINS.
        """
        with self.changed:
            self._settle()
            if self.generation != seen:
                return False
            if self.logins >= MAX_LOGINS:
                raise TokenExpiredError(f'{self.logins} logins used')
            self.logins += 1
            self.logging_in = True
            return True

    def finish_login(self, token: typing.Any) -> None:
        """Sign on with ``token``, with a new certificate on the next file."""
        with self.changed:
            self.token = token
            self.signer = None
            self.expires = None
            self.generation += 1
            self.logging_in = False
            self.changed.notify_all()

    def close(self) -> None:
        """Stop the run: waiting workers leave, nothing logs in again."""
        with self.changed:
            self.closed = True
            self.changed.notify_all()


class Throttle:
    """A pause every worker keeps before its next try, after a 429 or 5xx."""

    def __init__(self) -> None:
        """No pause yet."""
        self._lock: threading.Lock = threading.Lock()
        self._until: dt.datetime | None = None

    def hold(self, seconds: float) -> None:
        """Make every worker wait ``seconds`` from now, or longer if asked."""
        until: dt.datetime = now() + dt.timedelta(seconds=seconds)
        with self._lock:
            if self._until is None or until > self._until:
                self._until = until

    def _left(self) -> float:
        with self._lock:
            if self._until is None:
                return 0
            return (self._until - now()).total_seconds()

    def wait(self) -> None:
        """Sleep until the pause is over, also when it grows meanwhile."""
        left: float = self._left()
        while left > 0:
            sleep(left)
            left = self._left()


def _wait_for(error: Exception, delay: float) -> float | None:
    """Seconds to wait before the next try, None to give up now.

    A Retry-After longer than BACKOFF_MAX_SECONDS is honoured by stopping,
    not by a wait the run cannot afford.
    """
    asked: float | None = _retry_after(error)
    if asked is not None and asked > BACKOFF_MAX_SECONDS:
        return None
    return max(delay, asked or 0)


def _try_once(session: Session, entry: Entry) -> Signed | Exception:
    """The bundle of one try, or the 429 or 5xx error that is worth a retry.

    Every other error is raised.
    """
    try:
        return session.sign(entry.input)
    except Exception as error:
        if not retryable(http_status(error)):
            raise
        return error


def sign_with_backoff(
    session: Session, entry: Entry, throttle: Throttle
) -> Signed:
    """Sign one entry, backing off on 429 and 5xx, ATTEMPTS tries at most.

    The back-off goes through ``throttle``, so every worker waits it out.
    """
    delay: float = BACKOFF_START_SECONDS
    for attempt in range(1, ATTEMPTS + 1):
        throttle.wait()
        result: Signed | Exception = _try_once(session, entry)
        if isinstance(result, Signed):
            return result
        wait: float | None = _wait_for(result, delay)
        if attempt == ATTEMPTS or wait is None:
            raise RateLimitedError(str(result)) from result
        _say(
            RETRY_MESSAGE.format(
                status=http_status(result),
                name=entry.input.name,
                seconds=wait,
                attempt=attempt + 1,
                attempts=ATTEMPTS,
            )
        )
        throttle.hold(wait)
        delay = min(delay * 2, BACKOFF_MAX_SECONDS)
    raise AssertionError('unreachable')  # pragma: no cover


class TimestampWatch(logging.Handler):
    """Counts the timestamp failures sigstore only logs as warnings.

    ``sigstore sign`` writes the bundle without a signed timestamp when the
    authority fails, relying on the Rekor entry time, and so does this
    script. The count only slows the run down so the authority can recover.
    sigstore logs from the thread that signs, so each worker has a count.
    """

    def __init__(self) -> None:
        """Watch warnings and above, starting from zero failures."""
        super().__init__(logging.WARNING)
        self._counts: collections.Counter[int | None] = collections.Counter()
        self._count_lock: threading.Lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        """Count a warning that mentions the timestamp."""
        if 'timestamp' in record.getMessage():
            with self._count_lock:
                self._counts[record.thread] += 1

    def failures(self) -> int:
        """The failures logged so far by the calling thread."""
        with self._count_lock:
            return self._counts[threading.get_ident()]


@dataclasses.dataclass
class Progress:
    """How many entries of the manifest have a bundle."""

    total: int
    done: int
    signed: int = 0
    lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)

    def advance(self) -> None:
        """One more bundle, with a line every PROGRESS_EVERY and at the end."""
        with self.lock:
            self.done += 1
            self.signed += 1
            if self.signed % PROGRESS_EVERY == 0 or self.done == self.total:
                _say(PROGRESS_MESSAGE.format(done=self.done, total=self.total))

    def remaining(self) -> int:
        """Entries still without a bundle."""
        with self.lock:
            return self.total - self.done


class Batch:
    """The entries left to sign, and the error that stopped the run."""

    def __init__(self, todo: list[Entry]) -> None:
        """Sign ``todo`` in order."""
        self._lock: threading.Lock = threading.Lock()
        self._queue: collections.deque[Entry] = collections.deque(todo)
        self.error: Exception | None = None

    def take(self) -> Entry | None:
        """The next entry, None when there is none or the run stopped."""
        with self._lock:
            if self.error is not None or not self._queue:
                return None
            return self._queue.popleft()

    def put_back(self, entry: Entry) -> None:
        """Return an entry that was not signed, to be taken next."""
        with self._lock:
            self._queue.appendleft(entry)

    def left(self) -> int:
        """Entries not taken."""
        with self._lock:
            return len(self._queue)

    def fail(self, error: Exception) -> None:
        """Stop the run with ``error``, the first one wins."""
        with self._lock:
            if self.error is None:
                self.error = error


@dataclasses.dataclass
class Shared:
    """What the workers of one run share."""

    session: Session
    batch: Batch
    throttle: Throttle
    progress: Progress
    watch: TimestampWatch

    def stop(self, error: Exception) -> None:
        """Stop every worker, keeping ``error`` for the exit status."""
        self.batch.fail(error)
        self.session.close()


@dataclasses.dataclass
class Pace:
    """One worker's pause between files.

    PACE_SECONDS, or a growing back-off while the timestamp authority keeps
    failing for this worker's files.
    """

    pause: float | None = None
    cooldown: float = dataclasses.field(
        default_factory=lambda: BACKOFF_START_SECONDS
    )

    def wait(self) -> None:
        """Pause before the next file, not before the first one."""
        if self.pause is not None:
            sleep(self.pause)

    def after(self, entry: Entry, timestamp_failed: bool) -> None:
        """Set the pause after ``entry`` was signed."""
        if not timestamp_failed:
            self.pause = PACE_SECONDS
            self.cooldown = BACKOFF_START_SECONDS
            return
        self.pause = self.cooldown
        _say(
            TIMESTAMP_MESSAGE.format(name=entry.input.name, seconds=self.pause)
        )
        self.cooldown = min(self.cooldown * 2, BACKOFF_MAX_SECONDS)


def relogin(shared: Shared, seen: int) -> None:
    """Log in again after the token of generation ``seen`` expired.

    Only the first worker to see that token expire logs in, the others
    wait in ``Session.lease`` and sign on with the new token.
    """
    if not shared.session.begin_login(seen):
        return
    _say(RELOGIN_MESSAGE.format(remaining=shared.progress.remaining()))
    shared.session.finish_login(login(shared.session.trust))


def _sign_entry(shared: Shared, entry: Entry, pace: Pace) -> None:
    """Sign and write one entry, or put it back when the token expired."""
    seen: int = shared.session.generation
    failures: int = shared.watch.failures()
    try:
        signed: Signed = sign_with_backoff(
            shared.session, entry, shared.throttle
        )
    except TokenExpiredError:
        # Expiry is raised before sigstore makes a Rekor entry, so the
        # entry is unsigned and the next try cannot log it twice.
        shared.batch.put_back(entry)
        relogin(shared, seen)
        return
    write_bundle(entry.bundle, signed.bundle_json)
    shared.progress.advance()
    pace.after(entry, shared.watch.failures() > failures)


def _worker(shared: Shared) -> None:
    """Sign entries until the batch is empty or the run stopped."""
    pace: Pace = Pace()
    try:
        while (entry := shared.batch.take()) is not None:
            pace.wait()
            _sign_entry(shared, entry, pace)
    except StoppedError:
        return
    except Exception as error:
        shared.stop(error)


def sign_all(shared: Shared) -> None:
    """Sign every entry of the batch with WORKERS workers.

    Raises the error that stopped the run. A worker only leaves when the
    batch is empty, and an entry is only put back by a worker that goes
    on, so entries left over mean a bug and fail the run.
    """
    workers: list[threading.Thread] = [
        threading.Thread(
            target=_worker, args=(shared,), name=f'ppt-sigstore-{number}'
        )
        for number in range(1, max(1, WORKERS) + 1)
    ]
    for worker in workers:
        # Daemons, so an interrupt ends the run instead of waiting.
        worker.daemon = True
        worker.start()
    for worker in workers:
        worker.join()
    if shared.batch.error is not None:
        raise shared.batch.error
    if shared.batch.left():
        raise RuntimeError(f'{shared.batch.left()} files were left unsigned')


def _outcome(error: Exception, progress: Progress) -> tuple[int, str]:
    """The exit status and message for an error that stopped the run."""
    counts: dict[str, int] = {'done': progress.done, 'total': progress.total}
    if isinstance(error, TokenExpiredError):
        return EXIT_TOKEN_EXPIRED, TOKEN_EXPIRED_MESSAGE.format(**counts)
    if isinstance(error, RateLimitedError):
        return EXIT_RATE_LIMITED, RATE_LIMITED_MESSAGE.format(**counts)
    if isinstance(error, NoLoginError):
        return EXIT_NO_LOGIN, NO_LOGIN_MESSAGE.format(
            seconds=LOGIN_WAIT_SECONDS, **counts
        )
    detail: str = f'{type(error).__name__}: {error}'.rstrip(': ')
    return EXIT_FAILED, FAILED_MESSAGE.format(error=detail, **counts)


def run(entries: list[Entry]) -> tuple[int, str]:
    """Sign every entry without a bundle, return the exit status and message.

    Nothing logs in when every bundle is already there.
    """
    todo: list[Entry] = pending(entries)
    progress: Progress = Progress(len(entries), len(entries) - len(todo))
    if not todo:
        return EXIT_OK, DONE_MESSAGE.format(total=progress.total)
    watch: TimestampWatch = TimestampWatch()
    logger: logging.Logger = logging.getLogger(TIMESTAMP_LOGGER)
    logger.addHandler(watch)
    try:
        trust: typing.Any = load_trust()
        session: Session = Session(trust, login(trust))
        sign_all(Shared(session, Batch(todo), Throttle(), progress, watch))
    except Exception as error:
        return _outcome(error, progress)
    finally:
        logger.removeHandler(watch)
    return EXIT_OK, DONE_MESSAGE.format(total=progress.total)


def _parser() -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog='sigstore_signer',
        description=(
            'Sign the files of a manifest with Sigstore, a few at a time, '
            'logging in again in the browser when the identity token '
            'expires. Exit 3: out of logins. Exit 4: rate limited. Exit 5: '
            'no login arrived. All three keep the bundles written so far.'
        ),
    )
    parser.add_argument(
        '--manifest',
        required=True,
        type=pathlib.Path,
        help='JSON list of {"input": PATH, "bundle": PATH}',
    )
    parser.add_argument(
        '--status',
        type=pathlib.Path,
        help='file that receives the final message, for the caller',
    )
    return parser


def main(argv: list[str]) -> int:
    """Run the manifest, print the final message, return the exit status."""
    args: argparse.Namespace = _parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format='%(name)s: %(message)s')
    try:
        status, message = run(read_manifest(args.manifest))
    except ManifestError as error:
        status, message = EXIT_USAGE, str(error)
    _say(f'ppt: {message}')
    if args.status is not None:
        args.status.write_text(message + '\n', encoding='utf-8')
    return status


def leave(status: int) -> typing.NoReturn:
    """End the process with ``status``.

    After a login that never arrived, sigstore's redirect server still runs
    in a thread that is not a daemon, and a normal exit would wait for it
    forever. That exit skips the interpreter shutdown, the status file and
    every bundle are written by then.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    if status == EXIT_NO_LOGIN:
        os._exit(status)
    sys.exit(status)


if __name__ == '__main__':
    leave(main(sys.argv[1:]))
