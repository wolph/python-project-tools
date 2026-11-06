"""Tests for the gh-based GitHub client."""

from __future__ import annotations

import base64
import json
import pathlib

import pytest

from python_project_tools import common, github
from tests.fakes import Recorder, completed

REPO: str = 'WoLpH/spam'
WORKFLOWS: str = '.github/workflows'


def _release_json(tag: str, prerelease: bool = False) -> str:
    return json.dumps(
        {
            'tag_name': tag,
            'id': 7,
            'body': None,
            'prerelease': prerelease,
            'draft': False,
            'assets': [
                {
                    'name': 'spam-1.0.tar.gz',
                    'size': 12,
                    'browser_download_url': 'https://example.com/a.tar.gz',
                }
            ],
        }
    )


def test_tags_paginates_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    page1: str = json.dumps([{'name': 'v1.0', 'commit': {'sha': 'aaa'}}])
    page2: str = json.dumps([{'name': 'v0.9', 'commit': {'sha': 'bbb'}}])
    rec: Recorder = Recorder(
        {f'gh api --paginate repos/{REPO}/tags': completed(page1 + page2)}
    )
    monkeypatch.setattr(common, 'run', rec)
    assert github.tags(REPO) == [
        github.Tag('v1.0', 'aaa'),
        github.Tag('v0.9', 'bbb'),
    ]


def test_releases_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    rec: Recorder = Recorder(
        {
            f'gh api --paginate repos/{REPO}/releases': completed(
                f'[{_release_json("v1.0")}]'
            ),
        }
    )
    monkeypatch.setattr(common, 'run', rec)
    (release,) = github.releases(REPO)
    assert release.tag == 'v1.0'
    assert release.body == ''
    assert release.assets == [
        github.Asset('spam-1.0.tar.gz', 12, 'https://example.com/a.tar.gz')
    ]


def test_latest_release_none_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    rec: Recorder = Recorder(
        {f'gh api repos/{REPO}/releases/latest': completed('', 1)}
    )
    monkeypatch.setattr(common, 'run', rec)
    assert github.latest_release(REPO) is None


def test_create_release_passes_latest_false_and_verify_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec: Recorder = Recorder(
        {
            'gh release create v1.0': completed(),
            f'gh api repos/{REPO}/releases/tags/v1.0': completed(
                _release_json('v1.0')
            ),
        }
    )
    monkeypatch.setattr(common, 'run', rec)
    release: github.Release = github.create_release(
        REPO, 'v1.0', 'v1.0', 'notes', prerelease=False
    )
    argv: list[str] = rec.calls[0]
    assert '--latest=false' in argv
    assert '--verify-tag' in argv
    assert '--prerelease' not in argv
    assert release.tag == 'v1.0'


def test_create_release_marks_prerelease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec: Recorder = Recorder(
        {
            'gh release create': completed(),
            f'gh api repos/{REPO}/releases/tags/v1.0rc1': completed(
                _release_json('v1.0rc1', True)
            ),
        }
    )
    monkeypatch.setattr(common, 'run', rec)
    release: github.Release = github.create_release(
        REPO, 'v1.0rc1', 'v1.0rc1', 'notes', prerelease=True
    )
    assert '--prerelease' in rec.calls[0]
    assert release.prerelease


def test_create_release_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec: Recorder = Recorder({'gh release create': completed('', 1)})
    monkeypatch.setattr(common, 'run', rec)
    with pytest.raises(RuntimeError):
        github.create_release(REPO, 'v1', 'v1', 'n', prerelease=False)


def _content(text: str) -> str:
    return json.dumps({'content': base64.encodebytes(text.encode()).decode()})


def test_release_triggered_workflows_reads_active_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing: str = json.dumps(
        {
            'workflows': [
                {'path': f'{WORKFLOWS}/release.yml', 'state': 'active'},
                {
                    'path': f'{WORKFLOWS}/off.yml',
                    'state': 'disabled_manually',
                },
                {'path': f'{WORKFLOWS}/ci.yml', 'state': 'active'},
            ]
        }
    )
    base: str = f'gh api repos/{REPO}/contents/{WORKFLOWS}'
    rec: Recorder = Recorder(
        {
            f'gh api --paginate repos/{REPO}/actions/workflows': completed(
                listing
            ),
            f'{base}/release.yml': completed(
                _content('on:\n  release:\n    types: [published]\n')
            ),
            f'{base}/off.yml': completed(_content('on:\n  release:\n')),
            f'{base}/ci.yml': completed(
                _content('on:\n  push:\n    branches: [master]\n')
            ),
        }
    )
    monkeypatch.setattr(common, 'run', rec)
    assert github.release_triggered_workflows(REPO) == ['release.yml']
    assert not any('off.yml' in ' '.join(call) for call in rec.calls)


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('on:\n  release:\n    types: [created]\n', True),
        ('on:\n  release:\n', True),
        ('on:\n  release: {types: [created]}\n', True),
        ('on: [push, release]\n', True),
        ('on: release\n', True),
        ('on:\n  push:\n', False),
        ('on:\n  workflow_dispatch:\n# release: later\n', False),
    ],
)
def test_triggers_on_release(text: str, expected: bool) -> None:
    assert github._triggers_on_release(text) is expected


def test_upload_assets_clobber_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    rec: Recorder = Recorder({'gh release upload': completed()})
    monkeypatch.setattr(common, 'run', rec)
    paths: list[pathlib.Path] = [pathlib.Path('a.asc'), pathlib.Path('b.asc')]
    github.upload_assets(REPO, 'v1', paths, clobber=True)
    assert rec.calls[0] == [
        *('gh', 'release', 'upload', 'v1', '--repo', REPO),
        *('--clobber', 'a.asc', 'b.asc'),
    ]
    github.upload_assets(REPO, 'v1', paths)
    assert '--clobber' not in rec.calls[1]


def test_edit_notes_uses_notes_file(monkeypatch: pytest.MonkeyPatch) -> None:
    rec: Recorder = Recorder({'gh release edit v1 --repo': completed()})
    monkeypatch.setattr(common, 'run', rec)
    github.edit_notes(REPO, 'v1', 'body')
    argv: list[str] = rec.calls[0]
    assert argv[:6] == ['gh', 'release', 'edit', 'v1', '--repo', REPO]
    assert argv[6] == '--notes-file'


def test_default_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    rec: Recorder = Recorder(
        {f'gh api repos/{REPO}': completed('{"default_branch": "master"}')}
    )
    monkeypatch.setattr(common, 'run', rec)
    assert github.default_branch(REPO) == 'master'


def test_download_asset_uses_fetch_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    seen: list[tuple[str, pathlib.Path]] = []

    def fake(url: str, into: pathlib.Path) -> pathlib.Path:
        seen.append((url, into))
        return into

    monkeypatch.setattr(common, 'fetch_bytes', fake)
    asset: github.Asset = github.Asset('a.whl', 1, 'https://x/a.whl')
    assert github.download_asset(asset, tmp_path) == tmp_path / 'a.whl'
    assert seen == [('https://x/a.whl', tmp_path / 'a.whl')]


def test_recorder_unmatched_raises() -> None:
    with pytest.raises(AssertionError, match='no canned reply'):
        Recorder({})(['gh', 'nope'])
