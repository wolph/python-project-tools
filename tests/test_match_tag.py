"""Tests for ppt match-tag, run against a real scratch git repository."""

from __future__ import annotations

import datetime as dt
import pathlib
import shutil
import tarfile

import pytest

from python_project_tools import cli, common, match_tag, pypi, signing
from tests.conftest import ScratchRepo, git
from tests.fakes import Recorder, completed

needs_gpg = pytest.mark.skipif(
    shutil.which('gpg') is None, reason='gpg is not installed'
)

UTC: dt.timezone = dt.timezone.utc
EGG_INFO: str = (
    '[egg_info]\ntag_build = \ntag_date = 0\ntag_svn_revision = 0\n\n'
)


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


TAG_PUSH_CASES: list[tuple[str, bool]] = [
    # The 2020 pattern: a bare push trigger, the tag check inside the job.
    (
        'on: push\njobs:\n  publish:\n'
        "    if: startsWith(github.ref, 'refs/tags')\n",
        True,
    ),
    ('on: [push, pull_request]\n', True),
    ('on: [pull_request]\n', False),
    ('on: [pull_request,\n  create]\n', True),
    ('on: pull_request  # not push\n', False),
    ('on:\n  push:\n', True),
    ('on:\n  push:\n    paths: [spam]\n', True),
    ('on:\n  push:\n    branches: [master]\n', False),
    ('on:\n  push:\n    branches-ignore: [eggs]\n  pull_request:\n', False),
    ('on:\n  push:\n    branches: [master]\n    tags: ["v*"]\n', True),
    ('on:\n  push:\n    tags-ignore: [spam]\n', True),
    ('on:\n  push:\n    branches:\n    - master\n  release:\n', False),
    ('on:\n  create:\n', True),
    ('on:\n  - push\n  - pull_request\n', True),
    ('on:\n- pull_request\n- create\n', True),
    ('on:\n  pull_request:\n    branches: [master]\n', False),
    ('"on":\n  push:\n    branches:\n      - master\njobs: {}\n', False),
    ('on:\n  push: {branches: [master]}\n', False),
    ('on:\n  push: {tags: [v1]}\n', True),
    # A top-level flow map is read loosely: push in it always counts.
    ('on: {push: {branches: [master]}}\n', True),
    ('on: {pull_request: {branches: [master]}}\n', False),
    ('on: {pull_request: {}, create: {}}\n', True),
    ('on:\n  push:  # all\n    branches: [master]\n    # tags: [v1]\n', False),
    # No readable on key at all: over-report.
    ('jobs: {}\n', True),
]


@pytest.mark.parametrize(('text', 'expected'), TAG_PUSH_CASES)
def test_fires_on_tag_push(text: str, expected: bool) -> None:
    assert match_tag.fires_on_tag_push(text) is expected


def test_workflow_with_tag_trigger_reads_bare_push(
    scratch_repo: ScratchRepo,
) -> None:
    flows: pathlib.Path = scratch_repo.path / '.github' / 'workflows'
    flows.mkdir(parents=True)
    (flows / 'old.yml').write_text(
        'on: push\njobs:\n  publish:\n'
        "    if: startsWith(github.ref, 'refs/tags')\n"
    )
    (flows / 'ci.yaml').write_text('on:\n  push:\n    branches: [master]\n')
    (flows / 'notes.txt').write_text('on: push\n')
    git(scratch_repo.path, 'add', '-A')
    git(scratch_repo.path, 'commit', '-q', '-m', 'old workflow')
    head: str = git(scratch_repo.path, 'rev-parse', 'HEAD')
    assert match_tag.workflow_with_tag_trigger(scratch_repo.path, head) == [
        'old.yml'
    ]


def test_workflow_with_release_trigger_returns_paths(
    scratch_repo: ScratchRepo,
) -> None:
    flows: pathlib.Path = scratch_repo.path / '.github' / 'workflows'
    flows.mkdir(parents=True)
    (flows / 'publish.yml').write_text('on: {release: {types: [created]}}\n')
    (flows / 'ci.yml').write_text('on: [push]\n')
    git(scratch_repo.path, 'add', '-A')
    git(scratch_repo.path, 'commit', '-q', '-m', 'release workflow')
    head: str = git(scratch_repo.path, 'rev-parse', 'HEAD')
    found: list[str] = match_tag.workflow_with_release_trigger(
        scratch_repo.path, head
    )
    assert found == ['.github/workflows/publish.yml']
    assert (
        match_tag.workflow_with_release_trigger(
            scratch_repo.path, scratch_repo.shas[0]
        )
        == []
    )


def test_compare_counts_a_file_tracked_elsewhere_as_differing(
    scratch_repo: ScratchRepo, tmp_path: pathlib.Path
) -> None:
    # A is the 1.1 bump, B adds a file on top and the release came from B.
    path: pathlib.Path = scratch_repo.path
    bump: str = scratch_repo.shas[1]
    git(path, 'checkout', '-q', '-b', 'release-1.1', bump)
    (path / 'spam' / 'eggs.py').write_text('EGGS = 1\n')
    git(path, 'add', '-A')
    git(path, 'commit', '-q', '-m', 'add eggs')
    cut: str = git(path, 'rev-parse', 'HEAD')
    sdist: pathlib.Path = archive(
        scratch_repo, cut, tmp_path / 'spam-1.1.tar.gz'
    )
    at_bump: match_tag.Candidate = match_tag.compare(path, bump, sdist)
    assert at_bump.differing == ['spam/eggs.py']
    at_cut: match_tag.Candidate = match_tag.compare(path, cut, sdist)
    assert at_cut.differing == []
    uploaded: dt.datetime = at_bump.committed
    assert match_tag.choose([at_bump, at_cut], uploaded) is at_cut


def test_compare_skips_untracked_generated_and_build_files(
    scratch_repo: ScratchRepo, tmp_path: pathlib.Path
) -> None:
    # setup.cfg is tracked elsewhere in history, but not at the candidate,
    # so as a GENERATED name it is still skipped.
    path: pathlib.Path = scratch_repo.path
    git(path, 'checkout', '-q', '-b', 'later', scratch_repo.shas[2])
    (path / 'setup.cfg').write_text('[metadata]\nname = spam\n')
    git(path, 'add', '-A')
    git(path, 'commit', '-q', '-m', 'setup.cfg')
    sdist: pathlib.Path = archive(
        scratch_repo, scratch_repo.shas[1], tmp_path / 'plain.tar.gz'
    )
    extracted: pathlib.Path = tmp_path / 'x'
    with tarfile.open(sdist) as tar:
        tar.extractall(extracted, filter='data')
    top: pathlib.Path = extracted / 'spam-1.1'
    (top / 'PKG-INFO').write_text('Metadata-Version: 2.1\n')
    (top / 'setup.cfg').write_text('[egg_info]\ntag_build =\n')
    (top / 'spam' / '_build.py').write_text('generated\n')
    rebuilt: pathlib.Path = tmp_path / 'spam-1.1.tar.gz'
    with tarfile.open(rebuilt, 'w:gz') as tar:
        tar.add(top, arcname='spam-1.1')
    found: match_tag.Candidate = match_tag.compare(
        scratch_repo.path, scratch_repo.shas[1], rebuilt
    )
    assert found.differing == []
    assert found.matched == 3


def test_compare_reports_a_tracked_setup_cfg_that_differs(
    scratch_repo: ScratchRepo, tmp_path: pathlib.Path
) -> None:
    path: pathlib.Path = scratch_repo.path
    (path / 'setup.cfg').write_text('[metadata]\nname = spam\n')
    git(path, 'add', '-A')
    git(path, 'commit', '-q', '-m', 'setup.cfg')
    first: str = git(path, 'rev-parse', 'HEAD')
    (path / 'setup.cfg').write_text('[metadata]\nname = eggs\n')
    git(path, 'commit', '-q', '-am', 'rename')
    second: str = git(path, 'rev-parse', 'HEAD')
    sdist: pathlib.Path = archive(
        scratch_repo, second, tmp_path / 'spam-1.2.tar.gz'
    )
    assert match_tag.compare(path, first, sdist).differing == ['setup.cfg']
    assert match_tag.compare(path, second, sdist).differing == []


def rebuild_with_setup_cfg(
    repo: ScratchRepo, sha: str, tmp_path: pathlib.Path, text: str
) -> pathlib.Path:
    """An sdist of ``sha`` whose setup.cfg holds ``text``."""
    unpacked: pathlib.Path = tmp_path / 'unpacked'
    with tarfile.open(archive(repo, sha, tmp_path / 'a.tar.gz')) as tar:
        tar.extractall(unpacked, filter='data')
    (unpacked / 'spam-1.1' / 'setup.cfg').write_text(text)
    rebuilt: pathlib.Path = tmp_path / 'b.tar.gz'
    with tarfile.open(rebuilt, 'w:gz') as tar:
        tar.add(unpacked / 'spam-1.1', arcname='spam-1.1')
    return rebuilt


def test_compare_reports_a_real_setup_cfg_change_beside_egg_info(
    scratch_repo: ScratchRepo, tmp_path: pathlib.Path
) -> None:
    path: pathlib.Path = scratch_repo.path
    (path / 'setup.cfg').write_text('[metadata]\nname = spam\n')
    git(path, 'add', '-A')
    git(path, 'commit', '-q', '-m', 'setup.cfg')
    sha: str = git(path, 'rev-parse', 'HEAD')
    rebuilt: pathlib.Path = rebuild_with_setup_cfg(
        scratch_repo, sha, tmp_path, '[metadata]\nname = eggs\n\n' + EGG_INFO
    )
    assert match_tag.compare(path, sha, rebuilt).differing == ['setup.cfg']


def test_compare_ignores_the_egg_info_block_in_setup_cfg(
    scratch_repo: ScratchRepo, tmp_path: pathlib.Path
) -> None:
    path: pathlib.Path = scratch_repo.path
    (path / 'setup.cfg').write_text('')
    git(path, 'add', '-A')
    git(path, 'commit', '-q', '-m', 'empty setup.cfg')
    sha: str = git(path, 'rev-parse', 'HEAD')
    rebuilt: pathlib.Path = rebuild_with_setup_cfg(
        scratch_repo, sha, tmp_path, EGG_INFO
    )
    found: match_tag.Candidate = match_tag.compare(path, sha, rebuilt)
    assert found.differing == []
    assert found.matched >= 1


TISSUE_TRACKED: str = (
    '[nosetests]\nverbosity=3\ndetailed-errors=1\ndebug=nose.loader\n'
    'pdb=1\n\nwith-tissue=1\ntissue-fail-on-error=1\n'
)
TISSUE_SDIST: str = (
    '[nosetests]\nverbosity = 3\ndetailed-errors = 1\n'
    'debug = nose.loader\npdb = 1\nwith-tissue = 1\n'
    'tissue-fail-on-error = 1\n\n' + EGG_INFO
)


def test_setup_cfg_equivalent_for_the_tissue_0_9_1_rewrite() -> None:
    assert match_tag.setup_cfg_equivalent(TISSUE_TRACKED, TISSUE_SDIST)


def test_setup_cfg_equivalent_for_empty_against_egg_info_only() -> None:
    assert match_tag.setup_cfg_equivalent('', EGG_INFO)


def test_setup_cfg_not_equivalent_for_a_changed_value() -> None:
    changed: str = TISSUE_SDIST.replace('verbosity = 3', 'verbosity = 2')
    assert not match_tag.setup_cfg_equivalent(TISSUE_TRACKED, changed)


def test_setup_cfg_not_equivalent_for_an_extra_egg_info_key() -> None:
    extra: str = '[egg_info]\ntag_build = \negg_base = src\n'
    assert not match_tag.setup_cfg_equivalent('', extra)
    assert not match_tag.setup_cfg_equivalent(extra, EGG_INFO)


def test_setup_cfg_unparsable_falls_back_to_text() -> None:
    broken: str = 'no section header\n'
    assert match_tag.setup_cfg_equivalent(broken, broken + EGG_INFO)
    assert not match_tag.setup_cfg_equivalent(broken, 'other header\n')


def test_compare_accepts_the_tissue_0_9_1_rewrite(
    scratch_repo: ScratchRepo, tmp_path: pathlib.Path
) -> None:
    path: pathlib.Path = scratch_repo.path
    (path / 'setup.cfg').write_text(TISSUE_TRACKED)
    git(path, 'add', '-A')
    git(path, 'commit', '-q', '-m', 'nosetests')
    sha: str = git(path, 'rev-parse', 'HEAD')
    rebuilt: pathlib.Path = rebuild_with_setup_cfg(
        scratch_repo, sha, tmp_path, TISSUE_SDIST
    )
    assert match_tag.compare(path, sha, rebuilt).differing == []


def test_strip_egg_info_removes_the_setuptools_block() -> None:
    assert match_tag.strip_egg_info(EGG_INFO) == ''


def test_strip_egg_info_keeps_other_sections() -> None:
    text: str = '[metadata]\nname = spam\n\n' + EGG_INFO
    assert match_tag.strip_egg_info(text) == '[metadata]\nname = spam\n\n'


def test_strip_egg_info_keeps_a_block_with_real_configuration() -> None:
    text: str = '[egg_info]\ntag_build = dev\negg_base = src\n'
    assert match_tag.strip_egg_info(text) == text


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
    keys: list[str] = []
    monkeypatch.setattr(
        match_tag,
        'create_tag',
        lambda checkout, tag, sha, message, key: keys.append(key),
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
    allowed: list[str] = [*argv, '--allow-workflow-trigger']
    assert match_tag.main([*allowed, '--gpg-key', 'SPAM']) == 0
    assert pushed == ['v1.1']
    assert keys == ['SPAM']


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
        lambda checkout, tag, sha, msg, key: created.append(tag),
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
    # No user.signingkey: the key comes from the argument alone.
    match_tag.create_tag(
        scratch_repo.path,
        'v1.1',
        scratch_repo.shas[1],
        'spam message',
        key=gnupg_home,
    )
    assert git(scratch_repo.path, 'cat-file', '-t', 'v1.1') == 'tag'
    body: str = git(scratch_repo.path, 'cat-file', 'tag', 'v1.1')
    assert 'BEGIN PGP SIGNATURE' in body
    resolved: str = git(scratch_repo.path, 'rev-parse', 'v1.1^{}')
    assert resolved == scratch_repo.shas[1]


def test_create_tag_passes_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    rec: Recorder = Recorder({'git tag': completed()})
    monkeypatch.setattr(common, 'run', rec)
    match_tag.create_tag(pathlib.Path(), 'v1', 'abc', 'msg')
    match_tag.create_tag(pathlib.Path(), 'v2', 'abc', 'msg', key='SPAM')
    default: str = signing.DEFAULT_GPG_KEY
    assert rec.calls == [
        ['git', 'tag', '-s', '-u', default, 'v1', 'abc', '-m', 'msg'],
        ['git', 'tag', '-s', '-u', 'SPAM', 'v2', 'abc', '-m', 'msg'],
    ]


def test_match_tag_is_registered() -> None:
    assert cli.COMMANDS['match-tag'] is match_tag.main
