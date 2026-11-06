"""A small PyPI client: project metadata and sha256-verified downloads.

Call the network helpers as ``common.fetch_json(...)`` through the module,
so tests can replace them with one monkeypatch.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import pathlib
import re
import typing

from python_project_tools import common

PYPI_JSON_URL: str = 'https://pypi.org/pypi/{package}/json'
GITHUB_REPO: re.Pattern[str] = re.compile(r'github\.com/([\w.-]+)/([\w.-]+)')

# Projects whose PyPI metadata does not point at the right repository.
REPO_OVERRIDES: dict[str, str] = {
    'numpy-stl': 'WoLpH/numpy-stl',
    'portalocker': 'WoLpH/portalocker',
    'progressbar2': 'WoLpH/python-progressbar',
}

# Metadata fields scanned for a repository url, in order of preference.
_URL_FIELDS: tuple[str, ...] = ('home_page', 'project_url', 'download_url')


class IntegrityError(Exception):
    """A downloaded file does not match the sha256 PyPI published."""


@dataclasses.dataclass(frozen=True)
class PypiFile:
    """One uploaded file of a release."""

    filename: str
    url: str
    sha256: str
    size: int
    yanked: bool
    upload_time: dt.datetime  # timezone aware, UTC
    packagetype: str  # 'sdist' or 'bdist_wheel'


@dataclasses.dataclass(frozen=True)
class Project:
    """A PyPI project with every release and its files."""

    name: str
    latest: str
    releases: dict[str, list[PypiFile]]  # every version, empty list allowed
    repo: str | None  # 'Owner/name' from project URLs, overrides applied


def _parse_time(value: str) -> dt.datetime:
    """Parse an ISO 8601 timestamp, see common.parse_iso_datetime."""
    return common.parse_iso_datetime(value)


def _parse_file(raw: dict[str, object]) -> PypiFile:
    digests: dict[str, str] = typing.cast('dict[str, str]', raw['digests'])
    return PypiFile(
        filename=str(raw['filename']),
        url=str(raw['url']),
        sha256=digests['sha256'],
        size=int(typing.cast('int', raw['size'])),
        yanked=bool(raw.get('yanked', False)),
        upload_time=_parse_time(str(raw['upload_time_iso_8601'])),
        packagetype=str(raw['packagetype']),
    )


def _candidate_urls(info: dict[str, object]) -> list[str]:
    urls: list[str] = [
        value
        for field in _URL_FIELDS
        if isinstance(value := info.get(field), str)
    ]
    project_urls: object = info.get('project_urls')
    if isinstance(project_urls, dict):
        urls.extend(v for v in project_urls.values() if isinstance(v, str))
    return urls


def repo_from(info: dict[str, object], package: str) -> str | None:
    """Find the GitHub 'Owner/name' of a package from its PyPI metadata."""
    if package in REPO_OVERRIDES:
        return REPO_OVERRIDES[package]
    for url in _candidate_urls(info):
        match: re.Match[str] | None = GITHUB_REPO.search(url)
        if match is None or match.group(1) == 'sponsors':
            continue
        name: str = match.group(2).removesuffix('.git').rstrip('/')
        return f'{match.group(1)}/{name}'
    return None


def project(package: str) -> Project:
    """Fetch a project from PyPI.

    Raises LookupError when PyPI does not know it and common.FetchError on
    any other failure.
    """
    data: object | None = common.fetch_json(
        PYPI_JSON_URL.format(package=package)
    )
    if data is None:
        raise LookupError(f'{package} is not on PyPI')
    document: dict[str, object] = typing.cast('dict[str, object]', data)
    info: dict[str, object] = typing.cast(
        'dict[str, object]', document['info']
    )
    raw_releases: dict[str, list[dict[str, object]]] = typing.cast(
        'dict[str, list[dict[str, object]]]', document['releases']
    )
    releases: dict[str, list[PypiFile]] = {
        version: [_parse_file(raw) for raw in files]
        for version, files in raw_releases.items()
    }
    return Project(
        name=str(info['name']),
        latest=str(info['version']),
        releases=releases,
        repo=repo_from(info, package),
    )


def download(file: PypiFile, into: pathlib.Path) -> pathlib.Path:
    """Download a file into a directory and verify its sha256.

    A mismatching file is deleted and IntegrityError is raised.
    """
    into.mkdir(parents=True, exist_ok=True)
    target: pathlib.Path = common.fetch_bytes(file.url, into / file.filename)
    got: str = common.sha256_of(target)
    expected: str = file.sha256
    if got.lower() != expected.lower():
        target.unlink(missing_ok=True)
        raise IntegrityError(
            f'{file.filename}: sha256 {got} does not match PyPI {expected}'
        )
    return target


def files_for(project: Project, version: str) -> list[PypiFile]:
    """The files of one release, or an empty list for an unknown version."""
    return project.releases.get(version, [])
