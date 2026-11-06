"""Tests for the shared helpers in ``common``."""

from __future__ import annotations

import datetime as dt
import email.message
import hashlib
import io
import pathlib
import subprocess
import sys
import urllib.error
from collections.abc import Callable

import pytest

from python_project_tools import common


def _raising(error: Exception) -> Callable[..., object]:
    """A stand-in for urlopen that raises the given error."""

    def fake(*args: object, **kwargs: object) -> object:
        raise error

    return fake


def _responding(body: bytes) -> Callable[..., io.BytesIO]:
    """A stand-in for urlopen that returns a context manager over body."""

    def fake(*args: object, **kwargs: object) -> io.BytesIO:
        return io.BytesIO(body)

    return fake


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url='u',
        code=code,
        msg='',
        hdrs=email.message.Message(),
        fp=io.BytesIO(),
    )


def test_run_merges_env_over_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('PPT_SPAM', 'spam')
    script: str = (
        'import os; print(os.environ["PPT_SPAM"], os.environ["PPT_EGGS"])'
    )
    done: subprocess.CompletedProcess[str] = common.run(
        [sys.executable, '-c', script], env={'PPT_EGGS': 'eggs'}
    )
    assert done.stdout.split() == ['spam', 'eggs']


def test_run_without_capture_leaves_output_alone() -> None:
    done: subprocess.CompletedProcess[str] = common.run(
        [sys.executable, '-c', 'pass'], capture=False
    )
    assert done.returncode == 0
    assert done.stdout is None
    assert done.stderr is None


def test_run_bytes_returns_undecoded_output() -> None:
    script: str = 'import sys; sys.stdout.buffer.write(bytes([0, 255]))'
    assert common.run_bytes([sys.executable, '-c', script]) == b'\x00\xff'


def test_run_bytes_raises_with_stderr() -> None:
    script: str = 'import sys; sys.exit("spam went wrong")'
    with pytest.raises(RuntimeError, match='spam went wrong'):
        common.run_bytes([sys.executable, '-c', script])


def test_sha256_of_matches_hashlib(tmp_path: pathlib.Path) -> None:
    path: pathlib.Path = tmp_path / 'spam.txt'
    path.write_bytes(b'spam and eggs')
    expected: str = hashlib.sha256(b'spam and eggs').hexdigest()
    assert common.sha256_of(path) == expected


def test_fetch_json_returns_none_on_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(common, 'urlopen', _raising(_http_error(404)))
    assert common.fetch_json('https://example.invalid/x') is None


def test_fetch_json_raises_on_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(common, 'urlopen', _raising(_http_error(503)))
    with pytest.raises(common.FetchError, match=r'example\.invalid'):
        common.fetch_json('https://example.invalid/x')


def test_fetch_json_raises_on_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        common, 'urlopen', _raising(urllib.error.URLError('down'))
    )
    with pytest.raises(common.FetchError):
        common.fetch_json('https://example.invalid/x')


def test_fetch_json_raises_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(common, 'urlopen', _raising(TimeoutError('slow')))
    with pytest.raises(common.FetchError):
        common.fetch_json('https://example.invalid/x')


def test_fetch_json_raises_on_bad_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(common, 'urlopen', _responding(b'not json'))
    with pytest.raises(common.FetchError):
        common.fetch_json('https://example.invalid/x')


def test_fetch_json_parses_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, 'urlopen', _responding(b'{"spam": 1}'))
    assert common.fetch_json('https://example.invalid/x') == {'spam': 1}


def test_fetch_bytes_writes_the_body(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(common, 'urlopen', _responding(b'wheel bytes'))
    out: pathlib.Path = common.fetch_bytes(
        'https://example.invalid/w.whl', tmp_path / 'w.whl'
    )
    assert out.read_bytes() == b'wheel bytes'


def test_fetch_bytes_raises_on_error(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        common, 'urlopen', _raising(urllib.error.URLError('down'))
    )
    with pytest.raises(common.FetchError):
        common.fetch_bytes('https://example.invalid/w', tmp_path / 'w')


def test_results_are_keyword_constructible() -> None:
    result: common.Result = common.Result(
        name='tag', status='PASS', evidence='ok'
    )
    assert result.status == 'PASS'


def test_package_name_defaults_to_cwd_pyproject(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / 'pyproject.toml').write_text("[project]\nname = 'spam-eggs'\n")
    monkeypatch.chdir(tmp_path)
    assert common.read_package_name() == 'spam-eggs'


def test_package_name_is_empty_without_pyproject(
    tmp_path: pathlib.Path,
) -> None:
    assert common.read_package_name(tmp_path) == ''


def test_package_name_is_empty_without_name_key(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / 'pyproject.toml').write_text('[project]\nversion = "1"\n')
    assert common.read_package_name(tmp_path) == ''


class _FailingResponse(io.BytesIO):
    """A response that yields one chunk and then fails mid-body."""

    def read(self, size: int | None = -1) -> bytes:
        if self.tell():
            raise ConnectionResetError('reset')
        return super().read(size)


def test_fetch_json_wraps_read_phase_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake(*args: object, **kwargs: object) -> _FailingResponse:
        response: _FailingResponse = _FailingResponse(b'x')
        response.read()
        return response

    monkeypatch.setattr(common, 'urlopen', fake)
    with pytest.raises(common.FetchError, match='reset'):
        common.fetch_json('https://example.invalid/x')


def test_fetch_bytes_leaves_no_partial_file(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(*args: object, **kwargs: object) -> _FailingResponse:
        return _FailingResponse(b'partial')

    monkeypatch.setattr(common, 'urlopen', fake)
    target: pathlib.Path = tmp_path / 'w.whl'
    with pytest.raises(common.FetchError, match='reset'):
        common.fetch_bytes('https://example.invalid/w.whl', target)
    assert not target.exists()


def test_parse_iso_datetime_accepts_z_suffix() -> None:
    parsed: dt.datetime = common.parse_iso_datetime('2026-11-06T12:36:06Z')
    assert parsed == dt.datetime(
        2026, 11, 6, 12, 36, 6, tzinfo=dt.timezone.utc
    )


def test_parse_iso_datetime_converts_offset_to_utc() -> None:
    parsed: dt.datetime = common.parse_iso_datetime(
        '2026-11-06T12:00:00+02:00'
    )
    assert parsed.hour == 10
    assert parsed.utcoffset() == dt.timedelta(0)


def test_parse_iso_datetime_naive_gets_utc() -> None:
    parsed: dt.datetime = common.parse_iso_datetime('2026-11-06T12:00:00')
    assert parsed.tzinfo == dt.timezone.utc
    assert parsed.hour == 12
