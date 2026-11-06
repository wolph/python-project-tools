"""Releases and tags through the gh CLI.

Every call goes through ``common.run`` (or ``common.gh_json``, which uses
it), so tests replace one attribute with a recorder.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import json
import pathlib
import re
import subprocess
import tempfile
import typing

from python_project_tools import common

# Ways a workflow file can say it runs on a release event.
_ON_KEY: str = r'^[ \t]*(?:on|"on"|\'on\'):[ \t]*'
_RELEASE_TRIGGERS: tuple[re.Pattern[str], ...] = (
    re.compile(r'^[ \t]*release:[ \t]*(?:\{|#|$)', re.MULTILINE),
    re.compile(_ON_KEY + r'release[ \t]*(?:#|$)', re.MULTILINE),
    # Flow styles: on: [push, release] and on: {release: {...}}.
    re.compile(_ON_KEY + r'\[[^\]]*\brelease\b', re.MULTILINE),
    re.compile(_ON_KEY + r'\{.*\brelease\b', re.MULTILINE),
)
_DISABLED_PREFIX: str = 'disabled'


@dataclasses.dataclass(frozen=True)
class Tag:
    """A tag and the commit it points at."""

    name: str
    sha: str


@dataclasses.dataclass(frozen=True)
class Asset:
    """A file attached to a release."""

    name: str
    size: int
    url: str  # browser_download_url


@dataclasses.dataclass
class Release:
    """A GitHub release."""

    tag: str
    id: int
    body: str
    prerelease: bool
    draft: bool
    assets: list[Asset]


def _paginated(path: str) -> list[object]:
    """Run ``gh api --paginate`` and merge the pages.

    gh prints one JSON document per page, back to back. Array pages are
    concatenated, any other document is appended whole.
    """
    done: subprocess.CompletedProcess[str] = common.run(
        ['gh', 'api', '--paginate', path]
    )
    if done.returncode != 0:
        raise RuntimeError(f'gh api {path} failed: {done.stderr.strip()}')
    decoder: json.JSONDecoder = json.JSONDecoder()
    text: str = done.stdout
    items: list[object] = []
    position: int = 0
    while True:
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text):
            return items
        page: object
        page, position = decoder.raw_decode(text, position)
        if isinstance(page, list):
            items.extend(page)
        else:
            items.append(page)


def _asset(raw: dict[str, typing.Any]) -> Asset:
    return Asset(
        name=str(raw['name']),
        size=int(raw['size']),
        url=str(raw['browser_download_url']),
    )


def _release(raw: dict[str, typing.Any]) -> Release:
    return Release(
        tag=str(raw['tag_name']),
        id=int(raw['id']),
        body=str(raw.get('body') or ''),
        prerelease=bool(raw.get('prerelease', False)),
        draft=bool(raw.get('draft', False)),
        assets=[_asset(asset) for asset in raw.get('assets') or []],
    )


def tags(repo: str) -> list[Tag]:
    """Every tag of a repository."""
    raw: list[typing.Any] = typing.cast(
        'list[typing.Any]', _paginated(f'repos/{repo}/tags?per_page=100')
    )
    return [Tag(str(item['name']), str(item['commit']['sha'])) for item in raw]


def releases(repo: str) -> list[Release]:
    """Every release of a repository, drafts included when gh shows them."""
    raw: list[typing.Any] = typing.cast(
        'list[typing.Any]',
        _paginated(f'repos/{repo}/releases?per_page=100'),
    )
    return [_release(item) for item in raw]


def _not_found(done: subprocess.CompletedProcess[str]) -> bool:
    text: str = f'{done.stderr or ""}\n{done.stdout or ""}'
    return 'HTTP 404' in text or 'Not Found' in text


def latest_release(repo: str) -> Release | None:
    """The release GitHub marks as latest, or None when there is none.

    Only a 404 means "none". Any other failure raises RuntimeError, so an
    outage never reads as "this repository has no latest release".
    """
    path: str = f'repos/{repo}/releases/latest'
    done: subprocess.CompletedProcess[str] = common.run(['gh', 'api', path])
    if done.returncode != 0:
        if _not_found(done):
            return None
        raise RuntimeError(f'gh api {path} failed: {done.stderr.strip()}')
    try:
        raw: object = json.loads(done.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f'gh api {path}: {error}') from error
    if isinstance(raw, dict):
        return _release(typing.cast('dict[str, typing.Any]', raw))
    raise RuntimeError(f'gh api {path}: not a release object')


def mark_latest(repo: str, tag: str) -> None:
    """Mark a release as the latest one."""
    done: subprocess.CompletedProcess[str] = common.run(
        ['gh', 'release', 'edit', tag, '--repo', repo, '--latest']
    )
    if done.returncode != 0:
        raise RuntimeError(
            f'gh release edit {tag} --latest failed: {done.stderr.strip()}'
        )


def _write_notes(directory: str, notes: str) -> pathlib.Path:
    path: pathlib.Path = pathlib.Path(directory) / 'notes.md'
    path.write_text(notes, encoding='utf-8')
    return path


def create_release(
    repo: str, tag: str, title: str, notes: str, prerelease: bool
) -> Release:
    """Create a release for an existing tag and return it.

    Never marks the release as latest and refuses to create the tag.
    """
    with tempfile.TemporaryDirectory() as directory:
        notes_file: pathlib.Path = _write_notes(directory, notes)
        args: list[str] = [
            *('gh', 'release', 'create', tag),
            *('--repo', repo, '--title', title),
            *('--notes-file', str(notes_file)),
            *('--verify-tag', '--latest=false'),
        ]
        if prerelease:
            args.append('--prerelease')
        done: subprocess.CompletedProcess[str] = common.run(args)
    if done.returncode != 0:
        raise RuntimeError(
            f'gh release create {tag} failed: {done.stderr.strip()}'
        )
    raw: object | None = common.gh_json(
        ['api', f'repos/{repo}/releases/tags/{tag}']
    )
    if raw is None:
        raise RuntimeError(f'release {tag} not found after creating it')
    return _release(typing.cast('dict[str, typing.Any]', raw))


def upload_assets(
    repo: str,
    tag: str,
    paths: list[pathlib.Path],
    clobber: bool = False,
) -> None:
    """Attach files to a release in one gh call."""
    args: list[str] = ['gh', 'release', 'upload', tag, '--repo', repo]
    if clobber:
        args.append('--clobber')
    args.extend(str(path) for path in paths)
    done: subprocess.CompletedProcess[str] = common.run(args)
    if done.returncode != 0:
        raise RuntimeError(
            f'gh release upload {tag} failed: {done.stderr.strip()}'
        )


def edit_notes(repo: str, tag: str, body: str) -> None:
    """Replace the notes of a release."""
    with tempfile.TemporaryDirectory() as directory:
        notes_file: pathlib.Path = _write_notes(directory, body)
        done: subprocess.CompletedProcess[str] = common.run(
            [
                *('gh', 'release', 'edit', tag, '--repo', repo),
                *('--notes-file', str(notes_file)),
            ]
        )
    if done.returncode != 0:
        raise RuntimeError(
            f'gh release edit {tag} failed: {done.stderr.strip()}'
        )


def download_asset(asset: Asset, into: pathlib.Path) -> pathlib.Path:
    """Download a release asset into a directory."""
    into.mkdir(parents=True, exist_ok=True)
    return common.fetch_bytes(asset.url, into / asset.name)


def _triggers_on_release(text: str) -> bool:
    """Whether workflow YAML text lists a release event."""
    return any(pattern.search(text) for pattern in _RELEASE_TRIGGERS)


def _workflow_text(repo: str, path: str) -> str | None:
    raw: object | None = common.gh_json(
        ['api', f'repos/{repo}/contents/{path}']
    )
    if not isinstance(raw, dict) or 'content' not in raw:
        return None
    try:
        return base64.b64decode(str(raw['content'])).decode('utf-8')
    except (binascii.Error, UnicodeDecodeError):
        return None


def _workflow_states(repo: str) -> dict[str, str]:
    """``{path: state}`` of every workflow GitHub knows for a repository."""
    pages: list[typing.Any] = typing.cast(
        'list[typing.Any]',
        _paginated(f'repos/{repo}/actions/workflows'),
    )
    return {
        str(workflow['path']): str(workflow.get('state', ''))
        for page in pages
        for workflow in page.get('workflows', [])
    }


def active_workflow_paths(repo: str) -> set[str]:
    """Paths (``.github/workflows/x.yml``) of the active workflows."""
    return {
        path
        for path, state in _workflow_states(repo).items()
        if state == 'active'
    }


def disabled_workflow_paths(repo: str) -> set[str]:
    """Paths of the workflows GitHub lists as disabled.

    ``disabled_manually``, ``disabled_inactivity`` and ``disabled_fork``
    all count. A deleted or unlisted workflow is not in this set.
    """
    return {
        path
        for path, state in _workflow_states(repo).items()
        if state.startswith(_DISABLED_PREFIX)
    }


def release_triggered_workflows(repo: str) -> list[str]:
    """File names of active workflows that run on a release event."""
    names: list[str] = []
    for path in sorted(active_workflow_paths(repo)):
        text: str | None = _workflow_text(repo, path)
        if text is not None and _triggers_on_release(text):
            names.append(pathlib.PurePosixPath(path).name)
    return names


def default_branch(repo: str) -> str:
    """The default branch name of a repository."""
    raw: object | None = common.gh_json(['api', f'repos/{repo}'])
    if not isinstance(raw, dict) or 'default_branch' not in raw:
        raise RuntimeError(f'could not read the default branch of {repo}')
    return str(raw['default_branch'])
