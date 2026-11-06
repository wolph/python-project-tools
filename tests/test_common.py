"""Tests for the shared helpers in ``common``."""

from __future__ import annotations

import email.message
import hashlib
import io
import pathlib
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
