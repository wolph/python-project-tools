"""Tests for the PyPI client."""

from __future__ import annotations

import datetime as dt
import hashlib
import pathlib
from collections.abc import Callable

import pytest

from python_project_tools import common, pypi

FAKE_JSON: dict[str, object] = {
    'info': {
        'name': 'spam-eggs',
        'version': '1.1',
        'home_page': '',
        'project_urls': {'Source': 'https://github.com/WoLpH/spam-eggs/'},
    },
    'releases': {
        '1.0': [
            {
                'filename': 'spam_eggs-1.0.tar.gz',
                'url': 'https://files.example/spam_eggs-1.0.tar.gz',
                'digests': {'sha256': 'a' * 64},
                'size': 10,
                'yanked': False,
                'upload_time_iso_8601': '2024-01-02T03:04:05.123456Z',
                'packagetype': 'sdist',
            }
        ],
        '1.1': [
            {
                'filename': 'spam_eggs-1.1-py3-none-any.whl',
                'url': 'https://files.example/spam_eggs-1.1-py3-none-any.whl',
                'digests': {'sha256': 'b' * 64},
                'size': 20,
                'yanked': True,
                'upload_time_iso_8601': '2024-02-02T03:04:05.123456Z',
                'packagetype': 'bdist_wheel',
            }
        ],
        '0.9': [],
    },
}


def _file(sha256: str) -> pypi.PypiFile:
    return pypi.PypiFile(
        filename='spam_eggs-1.0.tar.gz',
        url='https://files.example/spam_eggs-1.0.tar.gz',
        sha256=sha256,
        size=11,
        yanked=False,
        upload_time=dt.datetime(2024, 1, 2, tzinfo=dt.timezone.utc),
        packagetype='sdist',
    )


def _writing(body: bytes) -> Callable[[str, pathlib.Path], pathlib.Path]:
    def fetch(url: str, into: pathlib.Path) -> pathlib.Path:
        into.write_bytes(body)
        return into

    return fetch


def test_project_parses_files_and_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(common, 'fetch_json', lambda url: FAKE_JSON)
    proj: pypi.Project = pypi.project('spam-eggs')
    assert proj.repo == 'WoLpH/spam-eggs'
    assert proj.latest == '1.1'
    assert sorted(proj.releases) == ['0.9', '1.0', '1.1']
    assert proj.releases['0.9'] == []
    assert proj.releases['1.1'][0].yanked is True
    assert proj.releases['1.0'][0].sha256 == 'a' * 64
    assert proj.releases['1.0'][0].upload_time.tzinfo is not None
    assert pypi.files_for(proj, '1.0') == proj.releases['1.0']
    assert pypi.files_for(proj, '9.9') == []


def test_repo_override_wins_over_sponsors_url() -> None:
    info: dict[str, object] = {
        'home_page': 'https://github.com/sponsors/wolph',
        'project_urls': {},
    }
    assert pypi.repo_from(info, 'portalocker') == 'WoLpH/portalocker'


def test_repo_from_skips_sponsors_without_override() -> None:
    info: dict[str, object] = {
        'home_page': 'https://github.com/sponsors/wolph',
        'project_urls': {'Source': 'https://github.com/WoLpH/other'},
    }
    assert pypi.repo_from(info, 'other') == 'WoLpH/other'


def test_repo_from_handles_dot_git_suffix() -> None:
    info: dict[str, object] = {
        'project_urls': {'Source': 'https://github.com/WoLpH/mt940.git'}
    }
    assert pypi.repo_from(info, 'mt-940') == 'WoLpH/mt940'


def test_project_raises_lookup_error_on_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(common, 'fetch_json', lambda url: None)
    with pytest.raises(LookupError):
        pypi.project('nope')


def test_download_verifies_sha256(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body: bytes = b'sdist bytes'
    monkeypatch.setattr(common, 'fetch_bytes', _writing(body))
    good: pypi.PypiFile = _file(
        sha256=hashlib.sha256(body).hexdigest().upper()
    )
    assert pypi.download(good, tmp_path).read_bytes() == body


def test_download_rejects_sha256_mismatch(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(common, 'fetch_bytes', _writing(b'tampered'))
    with pytest.raises(pypi.IntegrityError):
        pypi.download(_file(sha256='0' * 64), tmp_path)
    assert list(tmp_path.iterdir()) == []  # the bad file is removed
