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


LATEST: str = f'gh api repos/{REPO}/releases/latest'


@pytest.mark.parametrize(
    ('stdout', 'stderr'),
    [
        ('', 'gh: Not Found (HTTP 404)'),
        ('{"message": "Not Found", "status": "404"}', 'gh: HTTP 404'),
    ],
)
def test_latest_release_none_on_404(
    monkeypatch: pytest.MonkeyPatch, stdout: str, stderr: str
) -> None:
    rec: Recorder = Recorder({LATEST: completed(stdout, 1, stderr)})
    monkeypatch.setattr(common, 'run', rec)
    assert github.latest_release(REPO) is None


def test_latest_release_raises_on_other_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec: Recorder = Recorder(
        {LATEST: completed('', 1, 'gh: Bad credentials (HTTP 401)')}
    )
    monkeypatch.setattr(common, 'run', rec)
    with pytest.raises(RuntimeError, match='HTTP 401'):
        github.latest_release(REPO)
    rec.replies[LATEST] = completed('', 1, 'connection reset')
    with pytest.raises(RuntimeError, match='connection reset'):
        github.latest_release(REPO)
    rec.replies[LATEST] = completed('not json')
    with pytest.raises(RuntimeError):
        github.latest_release(REPO)


def test_latest_release_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    rec: Recorder = Recorder({LATEST: completed(_release_json('v2.0'))})
    monkeypatch.setattr(common, 'run', rec)
    found: github.Release | None = github.latest_release(REPO)
    assert found is not None
    assert found.tag == 'v2.0'


def test_mark_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    rec: Recorder = Recorder({'gh release edit': completed()})
    monkeypatch.setattr(common, 'run', rec)
    github.mark_latest(REPO, 'v1.0')
    assert rec.calls == [
        ['gh', 'release', 'edit', 'v1.0', '--repo', REPO, '--latest']
    ]
    rec.replies['gh release edit'] = completed('', 1, 'no such release')
    with pytest.raises(RuntimeError, match='no such release'):
        github.mark_latest(REPO, 'v1.0')


def test_workflow_paths_by_state(monkeypatch: pytest.MonkeyPatch) -> None:
    listing: str = json.dumps(
        {
            'workflows': [
                {'path': f'{WORKFLOWS}/a.yml', 'state': 'active'},
                {'path': f'{WORKFLOWS}/b.yml', 'state': 'disabled_manually'},
                {'path': f'{WORKFLOWS}/c.yml', 'state': 'disabled_inactivity'},
                {'path': f'{WORKFLOWS}/d.yml', 'state': 'deleted'},
            ]
        }
    )
    rec: Recorder = Recorder(
        {
            f'gh api --paginate repos/{REPO}/actions/workflows': completed(
                listing
            )
        }
    )
    monkeypatch.setattr(common, 'run', rec)
    assert github.active_workflow_paths(REPO) == {f'{WORKFLOWS}/a.yml'}
    assert github.disabled_workflow_paths(REPO) == {
        f'{WORKFLOWS}/b.yml',
        f'{WORKFLOWS}/c.yml',
    }


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
