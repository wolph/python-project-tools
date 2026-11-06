"""Tests for ppt match-tag, run against a real scratch git repository."""

from __future__ import annotations

import datetime as dt
import pathlib
import shutil

import pytest

from python_project_tools import cli, match_tag, pypi
from tests.conftest import ScratchRepo, git

needs_gpg = pytest.mark.skipif(
    shutil.which('gpg') is None, reason='gpg is not installed'
)

UTC: dt.timezone = dt.timezone.utc


def archive(repo: ScratchRepo, sha: str, target: pathlib.Path) -> pathlib.Path:
    git(
        repo.path,
        'archive',
        '--format=tar.gz',
        '--prefix=spam-1.1/',
        f'--output={target}',
        sha,
    )
    return target


def candidate(
    sha: str, hour: int, matched: int = 2, differing: list[str] | None = None
) -> match_tag.Candidate:
    return match_tag.Candidate(
        sha=sha,
        committed=dt.datetime(2020, 1, 1, hour, tzinfo=UTC),
        matched=matched,
        differing=differing or [],
    )


def test_candidates_finds_the_bump_commit(scratch_repo: ScratchRepo) -> None:
    found: list[str] = match_tag.candidates(scratch_repo.path, '1.1')
    assert scratch_repo.shas[1] in found
    assert scratch_repo.shas[0] not in found
    assert len(found) == len(set(found))


def test_compare_full_match_and_differing(
    scratch_repo: ScratchRepo, tmp_path: pathlib.Path
) -> None:
    sdist: pathlib.Path = archive(
        scratch_repo, scratch_repo.shas[1], tmp_path / 'spam-1.1.tar.gz'
    )
    full: match_tag.Candidate = match_tag.compare(
        scratch_repo.path, scratch_repo.shas[1], sdist
    )
    assert full.differing == []
    assert full.matched == 3
    assert full.committed.tzinfo is not None
    other: match_tag.Candidate = match_tag.compare(
        scratch_repo.path, scratch_repo.shas[2], sdist
    )
    assert sorted(other.differing) == ['setup.py', 'spam/__init__.py']
    assert other.matched == 1


def test_choose_requires_full_match_and_prefers_closest() -> None:
    uploaded: dt.datetime = dt.datetime(2020, 1, 1, 10, tzinfo=UTC)
    near: match_tag.Candidate = candidate('near', 9)
    far: match_tag.Candidate = candidate('far', 1)
    partial: match_tag.Candidate = candidate('partial', 10, differing=['a'])
    empty: match_tag.Candidate = candidate('empty', 10, matched=0)
    assert match_tag.choose([far, partial, empty, near], uploaded) is near
    assert match_tag.choose([partial, empty], uploaded) is None


def add_workflow(repo: ScratchRepo, amend: bool = False) -> str:
    flows: pathlib.Path = repo.path / '.github' / 'workflows'
    flows.mkdir(parents=True)
    (flows / 'publish.yml').write_text(
        'on:\n  push:\n    tags:\n      - "v*"\njobs: {}\n'
    )
    (flows / 'ci.yml').write_text('on:\n  push:\n    branches: [master]\n')
    git(repo.path, 'add', '-A')
    if amend:
        git(repo.path, 'commit', '-q', '--amend', '--no-edit')
    else:
        git(repo.path, 'commit', '-q', '-m', 'workflow')
    return git(repo.path, 'rev-parse', 'HEAD')


def test_workflow_with_tag_trigger_detects_publish(
    scratch_repo: ScratchRepo,
) -> None:
    with_flow: str = add_workflow(scratch_repo)
    found: list[str] = match_tag.workflow_with_tag_trigger(
        scratch_repo.path, with_flow
    )
    assert found == ['publish.yml']
    without: list[str] = match_tag.workflow_with_tag_trigger(
        scratch_repo.path, scratch_repo.shas[0]
    )
    assert without == []


def fake_pypi(
    monkeypatch: pytest.MonkeyPatch, repo: ScratchRepo, sha: str
) -> None:
    file: pypi.PypiFile = pypi.PypiFile(
        filename='spam-1.1.tar.gz',
        url='https://example.invalid/spam-1.1.tar.gz',
        sha256='ab' * 32,
        size=1,
        yanked=False,
        upload_time=dt.datetime.now(UTC),
        packagetype='sdist',
    )
    project: pypi.Project = pypi.Project(
        name='spam', latest='1.2', releases={'1.1': [file]}, repo=None
    )
    monkeypatch.setattr(pypi, 'project', lambda package: project)

    def download(file: pypi.PypiFile, into: pathlib.Path) -> pathlib.Path:
        into.mkdir(parents=True, exist_ok=True)
        return archive(repo, sha, into / file.filename)

    monkeypatch.setattr(pypi, 'download', download)


def test_main_refuses_push_when_workflow_would_fire(
    scratch_repo: ScratchRepo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The 1.1 bump commit itself carries a workflow that fires on tags.
    git(scratch_repo.path, 'reset', '-q', '--hard', scratch_repo.shas[1])
    flowed: str = add_workflow(scratch_repo, amend=True)
    fake_pypi(monkeypatch, scratch_repo, flowed)
    pushed: list[str] = []
    monkeypatch.setattr(
        match_tag, 'push_tag', lambda checkout, tag: pushed.append(tag)
    )
    monkeypatch.setattr(
        match_tag, 'create_tag', lambda checkout, tag, sha, message: None
    )
    argv: list[str] = [
        'spam',
        '1.1',
        '--checkout',
        str(scratch_repo.path),
        '--create',
        '--push',
    ]
    assert match_tag.main(argv) == 2
    assert 'allow-workflow-trigger' in capsys.readouterr().err
    assert pushed == []
    assert match_tag.main([*argv, '--allow-workflow-trigger']) == 0
    assert pushed == ['v1.1']


def test_main_without_match_exits_one(
    scratch_repo: ScratchRepo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The sdist comes from a commit that no ref reaches, so nothing matches.
    (scratch_repo.path / 'README.md').write_text('changed\n')
    git(scratch_repo.path, 'commit', '-q', '-am', 'dangling')
    dangling: str = git(scratch_repo.path, 'rev-parse', 'HEAD')
    git(scratch_repo.path, 'reset', '-q', '--hard', scratch_repo.shas[2])
    fake_pypi(monkeypatch, scratch_repo, dangling)
    argv: list[str] = ['spam', '1.1', '--checkout', str(scratch_repo.path)]
    assert match_tag.main(argv) == 1
    assert 'no commit matches 1.1' in capsys.readouterr().out


def test_main_match_without_create_exits_zero(
    scratch_repo: ScratchRepo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_pypi(monkeypatch, scratch_repo, scratch_repo.shas[1])
    argv: list[str] = ['spam', '1.1', '--checkout', str(scratch_repo.path)]
    assert match_tag.main(argv) == 0
    assert scratch_repo.shas[1] in capsys.readouterr().out
    assert git(scratch_repo.path, 'tag', '--list') == ''


def test_main_refuses_an_existing_tag(
    scratch_repo: ScratchRepo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git(scratch_repo.path, 'tag', 'v1.1', scratch_repo.shas[0])
    fake_pypi(monkeypatch, scratch_repo, scratch_repo.shas[1])
    created: list[str] = []
    monkeypatch.setattr(
        match_tag,
        'create_tag',
        lambda checkout, tag, sha, msg: created.append(tag),
    )
    argv: list[str] = [
        'spam',
        '1.1',
        '--checkout',
        str(scratch_repo.path),
        '--create',
    ]
    assert match_tag.main(argv) == 1
    err: str = capsys.readouterr().err
    assert f'tag v1.1 already exists at {scratch_repo.shas[0]}' in err
    assert created == []


def test_main_without_sdist_exits_one(
    scratch_repo: ScratchRepo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project: pypi.Project = pypi.Project(
        name='spam', latest='1.1', releases={'1.1': []}, repo=None
    )
    monkeypatch.setattr(pypi, 'project', lambda package: project)
    argv: list[str] = ['spam', '1.1', '--checkout', str(scratch_repo.path)]
    assert match_tag.main(argv) == 1
    assert 'sdist' in capsys.readouterr().out


@needs_gpg
def test_create_tag_is_signed_and_annotated(
    scratch_repo: ScratchRepo, gnupg_home: str
) -> None:
    git(scratch_repo.path, 'config', 'user.signingkey', gnupg_home)
    match_tag.create_tag(
        scratch_repo.path, 'v1.1', scratch_repo.shas[1], 'spam message'
    )
    assert git(scratch_repo.path, 'cat-file', '-t', 'v1.1') == 'tag'
    body: str = git(scratch_repo.path, 'cat-file', 'tag', 'v1.1')
    assert 'BEGIN PGP SIGNATURE' in body
    resolved: str = git(scratch_repo.path, 'rev-parse', 'v1.1^{}')
    assert resolved == scratch_repo.shas[1]


def test_match_tag_is_registered() -> None:
    assert cli.COMMANDS['match-tag'] is match_tag.main
