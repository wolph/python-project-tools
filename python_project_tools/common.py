"""Helpers shared by the ppt commands.

Everything that touches the outside world (subprocesses, the network, the
filesystem layout of a checkout) lives here, so tests can replace one
attribute on this module instead of patching every command. Call these as
``common.run(...)`` and never import the names directly, or a monkeypatch
on ``common`` will not reach the caller.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import http.client
import json
import os
import pathlib
import subprocess
import urllib.error
import urllib.request

from python_project_tools.toml_compat import tomllib

# Tests replace this attribute to stand in for the network.
urlopen = urllib.request.urlopen

USER_AGENT: str = 'python-project-tools'
FETCH_TIMEOUT: int = 60
CHUNK_SIZE: int = 1 << 20


def parse_iso_datetime(value: str) -> dt.datetime:
    """Parse an ISO 8601 timestamp into an aware UTC datetime.

    Python 3.10's fromisoformat rejects the trailing Z that git and PyPI
    print for UTC, so it is rewritten to +00:00 first. A naive value is
    taken as UTC and an offset value is converted to UTC.
    """
    if value.endswith('Z'):
        value = value[:-1] + '+00:00'
    parsed: dt.datetime = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


@dataclasses.dataclass
class Result:
    """Outcome of one check."""

    name: str
    status: str  # PASS, FAIL or SKIP, plus WARN, EDIT and INFO in conda-forge
    evidence: str


class FetchError(Exception):
    """A network or decode failure, with the url in the message."""


def run(
    args: list[str],
    check: bool = False,
    cwd: pathlib.Path | None = None,
    env: dict[str, str] | None = None,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a command and capture its output as text.

    ``env`` is merged over ``os.environ``. With ``capture=False`` the
    command talks to the terminal directly and stdout and stderr are None.
    """
    merged: dict[str, str] | None = (
        None if env is None else {**os.environ, **env}
    )
    return subprocess.run(
        args,
        capture_output=capture,
        text=True,
        check=check,
        cwd=cwd,
        env=merged,
    )


def run_bytes(args: list[str], cwd: pathlib.Path | None = None) -> bytes:
    """Run a command and return its stdout undecoded, raising on failure.

    For output that is not text, such as the blob of a binary file.
    """
    done: subprocess.CompletedProcess[bytes] = subprocess.run(
        args, capture_output=True, cwd=cwd
    )
    if done.returncode != 0:
        error: str = done.stderr.decode(errors='replace').strip()
        raise RuntimeError(f'{" ".join(args)}: {error}')
    return done.stdout


def gh_json(args: list[str]) -> object | None:
    """Run a gh command that prints JSON, or return None when it fails."""
    done: subprocess.CompletedProcess[str] = run(['gh', *args])
    if done.returncode != 0 or not done.stdout.strip():
        return None
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError:
        return None


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={'Cache-Control': 'no-cache', 'User-Agent': USER_AGENT},
    )


def _failure(url: str, error: Exception) -> FetchError:
    """Wrap a network, protocol or decode error with the url."""
    if isinstance(error, urllib.error.HTTPError):
        error.close()
        return FetchError(f'{url}: HTTP {error.code}')
    if isinstance(error, urllib.error.URLError):
        return FetchError(f'{url}: {error.reason}')
    return FetchError(f'{url}: {error}')


_READ_ERRORS: tuple[type[Exception], ...] = (
    OSError,  # URLError, HTTPError, TimeoutError, ConnectionResetError
    http.client.HTTPException,  # IncompleteRead and friends
)


def fetch_json(url: str) -> object | None:
    """Fetch a JSON document, or return None when the server says 404.

    Any other failure, including one while reading the body, raises
    FetchError, so an outage never reads as "not found".
    """
    try:
        with urlopen(_request(url), timeout=FETCH_TIMEOUT) as response:
            body: bytes = response.read()
        return json.loads(body.decode())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            error.close()
            return None
        raise _failure(url, error) from error
    except (*_READ_ERRORS, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _failure(url, error) from error


def fetch_bytes(url: str, into: pathlib.Path) -> pathlib.Path:
    """Download a url into a file and return the file's path.

    A failed download removes the partially written file.
    """
    try:
        with (
            urlopen(_request(url), timeout=FETCH_TIMEOUT) as response,
            into.open('wb') as handle,
        ):
            for chunk in iter(lambda: response.read(CHUNK_SIZE), b''):
                handle.write(chunk)
    except _READ_ERRORS as error:
        into.unlink(missing_ok=True)
        raise _failure(url, error) from error
    return into


def sha256_of(path: pathlib.Path) -> str:
    """The hex SHA-256 digest of a file."""
    digest: hashlib._Hash = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read_package_name(root: pathlib.Path = pathlib.Path('.')) -> str:
    """Return the name from ``root/pyproject.toml``, or '' when absent."""
    pyproject: pathlib.Path = root / 'pyproject.toml'
    project: dict[str, object] = {}
    if pyproject.exists():
        project = tomllib.loads(pyproject.read_text()).get('project', {})  # type: ignore[assignment]
    return str(project.get('name', ''))
