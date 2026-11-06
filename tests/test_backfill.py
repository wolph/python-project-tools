"""Tests for ppt backfill."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import pathlib
import subprocess

import pytest

from python_project_tools import (
    backfill,
    cli,
    common,
    github,
    match_tag,
    pypi,
    rebuild,
    sign,
    signing,
)
from tests.conftest import ScratchRepo, git
from tests.fakes import Recorder, completed

SHA: str = 'a' * 40
ROOT: str = 'r' * 40
UPLOADED: dt.datetime = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
Files = list[pypi.PypiFile] | list[pathlib.Path]


def pypi_file(
    name: str, yanked: bool = False, kind: str = 'sdist'
) -> pypi.PypiFile:
    return pypi.PypiFile(
        filename=name,
        url=f'https://files.example/{name}',
        sha256='0' * 64,
        size=1,
        yanked=yanked,
        upload_time=UPLOADED,
        packagetype=kind,
    )


def files_of(version: str, yanked: bool = False) -> list[pypi.PypiFile]:
    return [
        pypi_file(f'spam-{version}.tar.gz', yanked),
        pypi_file(f'spam-{version}-py3-none-any.whl', yanked, 'bdist_wheel'),
    ]


def project_with(
    releases: dict[str, list[pypi.PypiFile]],
) -> pypi.Project:
    return pypi.Project(
        name='spam', latest='9', releases=releases, repo='WoLpH/spam'
    )


def release(tag: str, body: str = '') -> github.Release:
    return github.Release(
        tag=tag, id=1, body=body, prerelease=False, draft=False, assets=[]
    )


def by_version(plans: list[backfill.Plan]) -> dict[str, backfill.Plan]:
    return {plan.version: plan for plan in plans}


class World:
    """Replacements for the outside world, with what they were asked."""

    def __init__(self) -> None:
        self.project: pypi.Project = project_with({})
        self.tags: list[github.Tag] = []
        self.releases: list[github.Release] = []
        self.latest: list[str | None] = ['v1.0', 'v1.0']
        self.created: list[tuple[str, str, str, bool]] = []
        self.signed: list[tuple[github.Release, Files, sign.SignOptions]] = []
        self.downloads: list[str] = []
        self.new_tags: list[tuple[str, str, str]] = []
        self.pushed: list[str] = []
        self.rebuilt: list[str] = []
        self.match_sha: str | None = SHA
        self.tag_workflows: list[str] = []
        self.rebuild_error: str | None = None
        self.rebuild_raises: dict[str, Exception] = {}
        self.sign_status: str = 'signed'
        self.rec: Recorder = Recorder({})
        self.repo_meta: object | None = {'archived': False}
        self.marked: list[str] = []
        # Create and mark calls in the order they happened.
        self.events: list[str] = []
        self.release_workflows: list[str] = []
        self.release_shas: list[str] = []
        self.disabled: set[str] = set()
        self.tag_keys: list[str] = []
        self.assets: dict[str, bytes] = {}
        self.asset_downloads: list[str] = []
        self.sleeps: list[float] = []


def _sign_release(
    world: World,
    release: github.Release,
    files: Files,
    workdir: pathlib.Path,
    options: sign.SignOptions,
) -> sign.SignOutcome:
    world.signed.append((release, files, options))
    return sign.SignOutcome(
        tag=release.tag,
        version='',
        uploaded=[] if options.dry_run else ['x.asc'],
        kept=[],
        conflicts=[],
        status=world.sign_status,
        reason='' if world.sign_status != 'failed' else 'boom',
    )


def _create_release(
    world: World,
    repo: str,
    tag: str,
    title: str,
    notes: str,
    prerelease: bool,
) -> github.Release:
    world.created.append((tag, title, notes, prerelease))
    world.events.append(f'create {tag}')
    return release(tag)


def _rebuild(
    world: World, checkout: pathlib.Path, tag: str, out: pathlib.Path
) -> list[pathlib.Path]:
    world.rebuilt.append(tag)
    if tag in world.rebuild_raises:
        raise world.rebuild_raises[tag]
    if world.rebuild_error:
        raise rebuild.RebuildError(world.rebuild_error)
    out.mkdir(parents=True, exist_ok=True)
    wheel: pathlib.Path = out / 'spam-1.0-py3-none-any.whl'
    wheel.write_bytes(b'whl')
    return [wheel]


def _latest(world: World, repo: str) -> github.Release | None:
    tag: str | None = world.latest.pop(0)
    return None if tag is None else release(tag)


def _choose(
    world: World, cands: list[match_tag.Candidate], uploaded: dt.datetime
) -> match_tag.Candidate | None:
    return None if world.match_sha is None else cands[0]


def install(
    monkeypatch: pytest.MonkeyPatch, world: World, fake_sign: bool = True
) -> World:
    world.rec = Recorder(
        {
            'git fetch': completed(),
            'git show': completed('', 128),
            'git rev-list': completed(ROOT + '\n'),
            'git rev-parse': completed('', 1),
        }
    )
    monkeypatch.setattr(common, 'run', world.rec)
    monkeypatch.setattr(pypi, 'project', lambda package: world.project)
    monkeypatch.setattr(backfill, 'sleep', world.sleeps.append)

    def download(file: pypi.PypiFile, into: pathlib.Path) -> pathlib.Path:
        world.downloads.append(file.filename)
        into.mkdir(parents=True, exist_ok=True)
        return into / file.filename

    monkeypatch.setattr(pypi, 'download', download)
    monkeypatch.setattr(github, 'tags', lambda repo: list(world.tags))
    monkeypatch.setattr(github, 'releases', lambda repo: list(world.releases))
    monkeypatch.setattr(
        github, 'latest_release', lambda repo: _latest(world, repo)
    )
    monkeypatch.setattr(github, 'release_triggered_workflows', lambda repo: [])
    monkeypatch.setattr(
        github,
        'create_release',
        lambda *a: _create_release(world, *a),
    )
    if fake_sign:
        monkeypatch.setattr(
            sign, 'sign_release', lambda *a: _sign_release(world, *a)
        )
    monkeypatch.setattr(rebuild, 'rebuild', lambda *a: _rebuild(world, *a))
    monkeypatch.setattr(match_tag, 'candidates', lambda checkout, v: [SHA])
    monkeypatch.setattr(
        match_tag,
        'compare',
        lambda checkout, sha, sdist: match_tag.Candidate(sha, UPLOADED, 1, []),
    )
    monkeypatch.setattr(match_tag, 'choose', lambda *a: _choose(world, *a))
    monkeypatch.setattr(
        match_tag,
        'workflow_with_tag_trigger',
        lambda checkout, sha: list(world.tag_workflows),
    )

    def create_tag(
        checkout: pathlib.Path,
        tag: str,
        sha: str,
        message: str,
        key: str = '',
    ) -> None:
        world.new_tags.append((tag, sha, message))
        world.tag_keys.append(key)
        # A tag that was just created exists in the checkout.
        world.rec.replies['git rev-parse'] = completed(sha + '\n')

    monkeypatch.setattr(match_tag, 'create_tag', create_tag)
    monkeypatch.setattr(
        match_tag, 'push_tag', lambda checkout, tag: world.pushed.append(tag)
    )
    install_github(monkeypatch, world)
    return world


def install_github(monkeypatch: pytest.MonkeyPatch, world: World) -> None:
    """The repository metadata, Latest marking and workflow fakes."""

    def gh_json(args: list[str]) -> object | None:
        assert args == ['api', 'repos/WoLpH/spam'], args
        return world.repo_meta

    def download_asset(
        asset: github.Asset, into: pathlib.Path
    ) -> pathlib.Path:
        world.asset_downloads.append(asset.name)
        into.mkdir(parents=True, exist_ok=True)
        target: pathlib.Path = into / asset.name
        target.write_bytes(world.assets[asset.name])
        return target

    monkeypatch.setattr(common, 'gh_json', gh_json)

    def mark_latest(repo: str, tag: str) -> None:
        world.marked.append(tag)
        world.events.append(f'mark {tag}')

    monkeypatch.setattr(github, 'mark_latest', mark_latest)
    monkeypatch.setattr(
        github, 'disabled_workflow_paths', lambda repo: set(world.disabled)
    )

    def release_trigger(checkout: pathlib.Path, sha: str) -> list[str]:
        world.release_shas.append(sha)
        return list(world.release_workflows)

    monkeypatch.setattr(
        match_tag, 'workflow_with_release_trigger', release_trigger
    )
    monkeypatch.setattr(github, 'download_asset', download_asset)


def options(
    tmp_path: pathlib.Path,
    only: str | None = None,
    dry_run: bool = False,
    allow_workflow_trigger: bool = False,
) -> backfill.BackfillOptions:
    return backfill.BackfillOptions(
        repo='WoLpH/spam',
        checkout=tmp_path,
        only=only,
        dry_run=dry_run,
        allow_workflow_trigger=allow_workflow_trigger,
    )


def test_preflight_refuses_enabled_release_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_github(monkeypatch, World())
    monkeypatch.setattr(
        github, 'release_triggered_workflows', lambda repo: ['release.yml']
    )
    assert backfill.preflight('WoLpH/spam') == [
        'release.yml is enabled and triggers on release events'
    ]


@pytest.mark.parametrize(
    ('meta', 'expected'),
    [
        ({'archived': False}, []),
        ({}, []),
        (
            {'archived': True},
            [
                'WoLpH/spam is archived, run: gh repo unarchive WoLpH/spam',
            ],
        ),
        (None, ['could not read repos/WoLpH/spam']),
    ],
)
def test_preflight_refuses_an_archived_repository(
    monkeypatch: pytest.MonkeyPatch,
    meta: object | None,
    expected: list[str],
) -> None:
    world: World = World()
    world.repo_meta = meta
    install_github(monkeypatch, world)
    monkeypatch.setattr(github, 'release_triggered_workflows', lambda repo: [])
    assert backfill.preflight('WoLpH/spam') == expected


def test_main_exits_2_for_an_archived_repository(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.0': files_of('1.0')})
    world.repo_meta = {'archived': True}
    assert backfill.main(['spam', '--checkout', str(tmp_path)]) == 2
    assert 'gh repo unarchive WoLpH/spam' in capsys.readouterr().err
    assert not world.created
    assert not world.rec.calls


def test_main_exits_2_before_creating_anything_when_preflight_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.0': files_of('1.0')})
    monkeypatch.setattr(
        github, 'release_triggered_workflows', lambda repo: ['release.yml']
    )
    status: int = backfill.main(['spam', '--checkout', str(tmp_path)])
    assert status == 2
    assert 'release.yml is enabled' in capsys.readouterr().err
    assert not world.created
    assert not world.signed
    assert not world.new_tags
    assert not world.rec.calls


def test_plan_versions_classifies() -> None:
    project: pypi.Project = project_with(
        {
            '1.0': files_of('1.0'),
            '1.1': files_of('1.1'),
            '1.2': files_of('1.2'),
            '1.3': [],
            '1.4': files_of('1.4', yanked=True),
            '2.0rc1': files_of('2.0rc1'),
            '0.5': [],
        }
    )
    tags: list[github.Tag] = [
        github.Tag(name, SHA)
        for name in (
            'v1.0',
            'v1.1',
            'v1.3',
            'v1.4',
            'v2.0rc1',
            'v0.9',
            'nightly',
        )
    ]
    plans: dict[str, backfill.Plan] = by_version(
        backfill.plan_versions(project, tags, [release('v1.0')])
    )
    assert list(plans) == [
        '0.5',
        '0.9',
        '1.0',
        '1.1',
        '1.2',
        '1.3',
        '1.4',
        '2.0rc1',
    ]
    got: dict[str, tuple[str | None, str, str, str]] = {
        v: (p.tag, p.tag_action, p.release_action, p.files_source)
        for v, p in plans.items()
    }
    assert got == {
        '0.5': (None, 'untagged', 'create', 'none'),
        '0.9': ('v0.9', 'exists', 'create', 'rebuild'),
        '1.0': ('v1.0', 'exists', 'exists', 'pypi'),
        '1.1': ('v1.1', 'exists', 'create', 'pypi'),
        '1.2': (None, 'create', 'create', 'pypi'),
        '1.3': ('v1.3', 'exists', 'create', 'rebuild'),
        '1.4': ('v1.4', 'exists', 'create', 'pypi'),
        '2.0rc1': ('v2.0rc1', 'exists', 'create', 'pypi'),
    }
    assert plans['1.4'].yanked
    assert not plans['1.1'].yanked
    assert plans['2.0rc1'].prerelease
    assert not plans['1.0'].prerelease


def test_plan_versions_puts_unparsable_versions_last() -> None:
    project: pypi.Project = project_with(
        {'spam-1': files_of('x'), '1.0': files_of('1.0')}
    )
    plans: list[backfill.Plan] = backfill.plan_versions(project, [], [])
    assert [p.version for p in plans] == ['1.0', 'spam-1']
    assert not plans[1].prerelease


def test_changelog_section_extracts_the_block(
    scratch_repo: ScratchRepo,
) -> None:
    path: pathlib.Path = scratch_repo.path
    (path / 'CHANGELOG.rst').write_text(
        'Changelog\n=========\n\n'
        '1.2:\n\n- fixed spam\n- added eggs\n\n'
        '1.1:\n\n- older\n'
    )
    git(path, 'add', 'CHANGELOG.rst')
    git(path, 'commit', '-q', '-m', 'changelog')
    git(path, 'tag', 'v1.2')
    assert backfill.changelog_section(path, 'v1.2', '1.2') == (
        '1.2:\n\n- fixed spam\n- added eggs'
    )
    assert backfill.changelog_section(path, 'v1.2', '1.1') == (
        '1.1:\n\n- older'
    )
    assert backfill.changelog_section(path, 'v1.2', '0.1') is None
    assert backfill.changelog_section(path, 'v1.0', '1.0') is None


def test_changelog_section_reads_markdown_headings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    text: str = (
        '# Changes\n\n## v1.2\n\nspam\n\n### Fixed\n\neggs\n\n'
        '## 1.1\n\nolder\n'
    )

    def run(
        args: list[str],
        check: bool = False,
        cwd: pathlib.Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if args[:2] == ['git', 'rev-parse']:
            return completed(SHA)
        found: bool = args == ['git', 'show', 'v1.2:CHANGES.md']
        return completed(text if found else '', 0 if found else 128)

    monkeypatch.setattr(common, 'run', run)
    assert backfill.changelog_section(tmp_path, 'v1.2', '1.2') == (
        '## v1.2\n\nspam\n\n### Fixed\n\neggs'
    )


def plan_for(version: str = '1.0', yanked: bool = False) -> backfill.Plan:
    return backfill.Plan(
        version=version,
        tag=f'v{version}',
        tag_action='exists',
        release_action='create',
        files_source='pypi',
        prerelease=False,
        yanked=yanked,
    )


def test_notes_for_falls_back_to_stock_sentence() -> None:
    assert backfill.notes_for(plan_for('1.0'), None) == (
        'Release 1.0. No changelog section was found at tag v1.0.'
    )


def test_notes_for_uses_the_section_and_flags_yanked() -> None:
    assert backfill.notes_for(plan_for(), '1.0:\n\n- spam') == (
        '1.0:\n\n- spam'
    )
    yanked: str = backfill.notes_for(plan_for(yanked=True), '1.0:\n\n- spam')
    assert yanked.splitlines()[0] == 'This version was yanked on PyPI.'
    assert yanked.endswith('1.0:\n\n- spam')


def test_run_backfill_dry_run_creates_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world: World = install(monkeypatch, World(), fake_sign=False)
    world.project = project_with(
        {'1.0': files_of('1.0'), '1.1': files_of('1.1'), '1.2': []}
    )
    world.tags = [github.Tag('v1.0', SHA), github.Tag('v1.2', SHA)]
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path, dry_run=True)
    )
    printed: str = capsys.readouterr().out
    for phrase in (
        'would match tag against sdist',
        'would create tag v1.1',
        'would create release v1.0',
        'would download 2 files',
        'would rebuild',
        'would sign',
        'would upload spam-1.0.tar.gz.asc',
    ):
        assert phrase in printed
    assert not world.created
    assert not world.new_tags
    assert not world.pushed
    assert not world.downloads
    assert not world.rebuilt
    assert world.rec.calls == []
    assert len(report.rows) == 3


def test_run_backfill_creates_tag_release_and_signs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.2rc1': files_of('1.2rc1')})
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert world.downloads == ['spam-1.2rc1.tar.gz']
    tag, sha, message = world.new_tags[0]
    assert (tag, sha) == ('v1.2rc1', SHA)
    assert message.startswith('Retroactive tag for PyPI release spam 1.2rc1')
    assert world.pushed == ['v1.2rc1']
    created_tag, title, notes, prerelease = world.created[0]
    assert (created_tag, title, prerelease) == ('v1.2rc1', 'v1.2rc1', True)
    assert 'No changelog section' in notes
    signed_release, files, _ = world.signed[0]
    assert signed_release.tag == 'v1.2rc1'
    assert files == world.project.releases['1.2rc1']
    plan, outcome, note = report.rows[0]
    assert plan.tag == 'v1.2rc1'
    assert outcome is not None
    assert outcome.status == 'signed'
    assert outcome.version == '1.2rc1'
    assert note == ''


def test_run_backfill_signs_an_existing_release_without_creating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.0': files_of('1.0')})
    world.tags = [github.Tag('v1.0', SHA)]
    world.releases = [release('v1.0', body='mine')]
    backfill.run_backfill('spam', options(tmp_path))
    assert not world.created
    assert world.signed[0][0].body == 'mine'


def test_workflow_that_would_fire_skips_the_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.2': files_of('1.2')})
    world.tag_workflows = ['publish.yml']
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert not world.new_tags
    assert not world.created
    assert not world.signed
    _, outcome, note = report.rows[0]
    assert outcome is None
    assert 'tag not pushed, workflow would fire' in note
    world.latest = ['v1.0', 'v1.0']
    allowed: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path, allow_workflow_trigger=True)
    )
    assert world.pushed == ['v1.2']
    assert allowed.rows[0][1] is not None


def test_run_backfill_asserts_latest_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.0': files_of('1.0')})
    world.tags = [github.Tag('v1.0', SHA)]
    world.latest = ['v1.0', 'v1.1']
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert (report.latest_before, report.latest_after) == ('v1.0', 'v1.1')
    assert 'Latest before: v1.0, after: v1.1' in backfill.to_markdown(report)
    assert backfill.to_markdown(report).rstrip().endswith('CHANGED')
    world.latest = ['v1.0', 'v1.1']
    status: int = backfill.main(['spam', '--checkout', str(tmp_path)])
    assert status == 1
    assert 'CHANGED' in capsys.readouterr().out


def test_main_exits_0_when_everything_is_signed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.0': files_of('1.0')})
    world.tags = [github.Tag('v1.0', SHA)]
    assert backfill.main(['spam', '--checkout', str(tmp_path)]) == 0
    printed: str = capsys.readouterr().out
    assert '| version |' in printed
    assert printed.rstrip().endswith('OK')


def test_main_exits_1_when_a_version_fails_or_is_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.0': files_of('1.0')})
    world.tags = [github.Tag('v1.0', SHA)]
    world.sign_status = 'failed'
    assert backfill.main(['spam', '--checkout', str(tmp_path)]) == 1
    world = install(monkeypatch, World())
    world.project = project_with({'0.5': []})
    assert backfill.main(['spam', '--checkout', str(tmp_path)]) == 1


def test_untagged_versions_land_on_the_untagged_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with(
        {'1.0': files_of('1.0'), '1.1': files_of('1.1')}
    )
    world.match_sha = None
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert world.new_tags == [
        (
            backfill.UNTAGGED_TAG,
            ROOT,
            'Anchor for PyPI versions whose commit could not be matched.',
        )
    ]
    assert world.pushed == [backfill.UNTAGGED_TAG]
    tag, title, notes, prerelease = world.created[0]
    assert (tag, title, prerelease) == (
        backfill.UNTAGGED_TAG,
        backfill.UNTAGGED_TITLE,
        False,
    )
    assert '- 1.0\n- 1.1' in notes
    assert len(world.signed) == 1
    signed_release, files, _ = world.signed[0]
    assert signed_release.tag == backfill.UNTAGGED_TAG
    assert files == files_of('1.0') + files_of('1.1')
    assert len(report.rows) == 1
    plan, outcome, _ = report.rows[0]
    assert plan.version == '1.0, 1.1'
    assert plan.tag == backfill.UNTAGGED_TAG
    assert outcome is not None


def test_untagged_release_reuses_an_existing_anchor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.0': files_of('1.0')})
    world.match_sha = None
    world.tags = [github.Tag(backfill.UNTAGGED_TAG, ROOT)]
    world.releases = [release(backfill.UNTAGGED_TAG)]
    backfill.run_backfill('spam', options(tmp_path))
    assert not world.new_tags
    assert not world.pushed
    assert not world.created
    assert world.signed[0][0].tag == backfill.UNTAGGED_TAG


def test_rebuild_failure_creates_no_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.0': []})
    world.tags = [github.Tag('v1.0', SHA)]
    world.rebuild_error = 'uv build failed'
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert not world.created
    assert not world.signed
    _, outcome, note = report.rows[0]
    assert outcome is None
    assert 'uv build failed' in note


def test_rebuilt_files_are_signed_with_a_disclosure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.0': []})
    world.tags = [github.Tag('v1.0', SHA)]
    backfill.run_backfill('spam', options(tmp_path))
    assert world.rebuilt == ['v1.0']
    assert world.created[0][0] == 'v1.0'
    _, files, sign_options = world.signed[0]
    assert [p.name for p in files if isinstance(p, pathlib.Path)] == [
        'spam-1.0-py3-none-any.whl'
    ]
    note: str = sign_options.rebuilt['spam-1.0-py3-none-any.whl']
    assert note.startswith('# rebuilt from tag v1.0 on ')
    assert note.endswith(', not the PyPI bytes')


def test_only_limits_the_run_to_one_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with(
        {'1.0': files_of('1.0'), '1.1': files_of('1.1')}
    )
    world.tags = [github.Tag('v1.0', SHA), github.Tag('v1.1', SHA)]
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path, only='1.1')
    )
    assert [plan.version for plan, _, _ in report.rows] == ['1.1']
    world.latest = ['v1.0', 'v1.0']
    with pytest.raises(LookupError):
        backfill.run_backfill('spam', options(tmp_path, only='9.9'))


def test_changelog_text_becomes_the_release_notes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.rec.replies['git rev-parse'] = completed(SHA)
    world.rec.replies['git show v1.0:CHANGELOG.rst'] = completed(
        '1.0:\n\n- spam\n\n0.9:\n\n- eggs\n'
    )
    world.project = project_with({'1.0': files_of('1.0')})
    world.tags = [github.Tag('v1.0', SHA)]
    backfill.run_backfill('spam', options(tmp_path))
    assert world.created[0][2] == '1.0:\n\n- spam'


def test_to_markdown_lists_every_column() -> None:
    plan: backfill.Plan = plan_for('1.0')
    outcome: sign.SignOutcome = sign.SignOutcome(
        tag='v1.0',
        version='1.0',
        uploaded=['a', 'b'],
        kept=['c'],
        conflicts=[],
        status='signed',
        reason='',
    )
    skipped: backfill.Plan = dataclasses.replace(
        plan_for('1.1'), tag=None, tag_action='untagged'
    )
    text: str = backfill.to_markdown(
        backfill.BackfillReport(
            package='spam',
            repo='WoLpH/spam',
            rows=[(plan, outcome, ''), (skipped, None, 'no files | none')],
            latest_before=None,
            latest_after=None,
        )
    )
    header: str = text.splitlines()[0]
    for column in (
        'version',
        'tag',
        'tag action',
        'release action',
        'files',
        'status',
        'uploaded',
        'kept',
        'conflicts',
        'note',
    ):
        assert column in header
    assert '| 1.0 | v1.0 | exists | create | pypi | signed | 2 | 1 | 0 |' in (
        text
    )
    assert '| skipped |' in text
    assert 'no files \\| none' in text
    assert 'Latest before: none, after: none' in text
    assert text.rstrip().endswith('OK')


def test_backfill_is_registered() -> None:
    assert cli.COMMANDS['backfill'] is backfill.main


def test_changelog_section_tolerates_a_non_utf8_changelog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    def run(
        args: list[str],
        check: bool = False,
        cwd: pathlib.Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if args[:2] == ['git', 'rev-parse']:
            return completed(SHA)
        raise UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid start byte')

    monkeypatch.setattr(common, 'run', run)
    assert backfill.changelog_section(tmp_path, 'v1.2', '1.2') is None


def test_changelog_section_is_none_when_the_tag_is_not_local(
    scratch_repo: ScratchRepo,
) -> None:
    assert backfill.changelog_section(scratch_repo.path, 'v9', '9') is None


def test_an_oserror_fails_one_version_and_the_loop_continues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.0': [], '1.1': files_of('1.1')})
    world.tags = [github.Tag('v1.0', SHA), github.Tag('v1.1', SHA)]
    world.rebuild_raises['v1.0'] = FileNotFoundError('uv: not found')
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    first, second = report.rows
    assert first[1] is None
    assert 'uv: not found' in first[2]
    assert second[1] is not None
    assert second[1].status == 'signed'
    assert [created[0] for created in world.created] == ['v1.1']


def test_a_stray_key_error_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.0': []})
    world.tags = [github.Tag('v1.0', SHA)]
    world.rebuild_raises['v1.0'] = KeyError('bug')
    with pytest.raises(KeyError):
        backfill.run_backfill('spam', options(tmp_path))


def test_tags_are_fetched_first_unless_dry_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.0': files_of('1.0')})
    world.tags = [github.Tag('v1.0', SHA)]
    backfill.run_backfill('spam', options(tmp_path))
    assert world.rec.calls[0] == ['git', 'fetch', 'origin', '--tags']
    world.rec.calls.clear()
    world.latest = ['v1.0', 'v1.0']
    backfill.run_backfill('spam', options(tmp_path, dry_run=True))
    assert not any(call[:2] == ['git', 'fetch'] for call in world.rec.calls)


def test_a_failed_fetch_exits_2_before_anything_is_created(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world: World = install(monkeypatch, World())
    world.rec.replies['git fetch'] = subprocess.CompletedProcess(
        [], 1, '', 'no remote'
    )
    world.project = project_with({'1.0': files_of('1.0')})
    world.tags = [github.Tag('v1.0', SHA)]
    assert backfill.main(['spam', '--checkout', str(tmp_path)]) == 2
    assert 'no remote' in capsys.readouterr().err
    assert not world.created
    assert not world.signed


def test_a_tag_missing_from_the_checkout_gets_stock_notes_and_a_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.0': files_of('1.0')})
    world.tags = [github.Tag('v1.0', SHA)]
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert 'No changelog section' in world.created[0][2]
    assert report.rows[0][2] == 'tag not in checkout, stock notes used'


def test_a_local_tag_at_the_matched_commit_is_pushed_not_recreated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.rec.replies['git rev-parse'] = completed(SHA + '\n')
    world.project = project_with({'1.2': files_of('1.2')})
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert not world.new_tags
    assert world.pushed == ['v1.2']
    assert world.created[0][0] == 'v1.2'
    assert report.rows[0][2] == ''


def test_a_local_tag_elsewhere_skips_the_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    other: str = 'b' * 40
    world: World = install(monkeypatch, World())
    world.rec.replies['git rev-parse'] = completed(other + '\n')
    world.project = project_with({'1.2': files_of('1.2')})
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert not world.new_tags
    assert not world.pushed
    assert not world.created
    _, outcome, note = report.rows[0]
    assert outcome is None
    assert note == (
        f'local tag v1.2 points at {other}, not at the matched commit'
    )


def test_an_unreadable_latest_after_the_run_reports_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.0': files_of('1.0')})
    world.tags = [github.Tag('v1.0', SHA)]
    calls: list[int] = []

    def latest(repo: str) -> github.Release | None:
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError('gh is down')
        return release('v1.0')

    monkeypatch.setattr(github, 'latest_release', latest)
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert report.latest_after == 'unknown'
    text: str = backfill.to_markdown(report)
    assert 'Latest before: v1.0, after: unknown' in text
    assert text.rstrip().endswith('CHANGED')


def tagged(world: World, *names: str) -> None:
    """Tags for these versions on GitHub, no releases."""
    world.tags = [github.Tag(f'v{name}', SHA) for name in names]


def test_no_latest_before_releases_the_highest_stable_first(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with(
        {
            '1.0': files_of('1.0'),
            '1.1': files_of('1.1'),
            '1.2rc1': files_of('1.2rc1'),
            '1.3': files_of('1.3', yanked=True),
        }
    )
    tagged(world, '1.0', '1.1', '1.2rc1', '1.3')
    # Before the run, after the first release, before marking, after the run.
    world.latest = [None, 'v1.1', 'v1.1', 'v1.1']
    assert backfill.main(['spam', '--checkout', str(tmp_path)]) == 0
    assert [created[0] for created in world.created] == [
        'v1.1',
        'v1.0',
        'v1.2rc1',
        'v1.3',
    ]
    # Pinned once right after the first release, before the second.
    assert world.marked == ['v1.1']
    assert world.events[:3] == ['create v1.1', 'mark v1.1', 'create v1.0']
    printed: str = capsys.readouterr().out
    assert 'Latest before: none, after: v1.1, target: v1.1' in printed
    assert printed.rstrip().endswith('OK')


def test_no_latest_before_and_latest_elsewhere_after_is_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with(
        {'1.0': files_of('1.0'), '1.1': files_of('1.1')}
    )
    tagged(world, '1.0', '1.1')
    # Latest moved off the target after the pin, so it is marked again.
    world.latest = [None, 'v1.1', 'v1.0', 'v1.0']
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert world.marked == ['v1.1', 'v1.1']
    assert report.latest_target == 'v1.1'
    assert backfill.to_markdown(report).rstrip().endswith('CHANGED')
    assert backfill._exit_status(report) == 1


def test_no_latest_before_and_only_prereleases_marks_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'2.0rc1': files_of('2.0rc1')})
    tagged(world, '2.0rc1')
    # Before the run, five reads after the first release, after the run.
    world.latest = [None] * 7
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert world.sleeps == [2.0] * 4
    assert world.marked == []
    assert report.latest_target is None
    text: str = backfill.to_markdown(report)
    assert 'Latest before: none, after: none, target: none' in text
    assert text.rstrip().endswith('OK')


def test_latest_appearing_mid_run_stops_with_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with(
        {'1.0': files_of('1.0'), '1.1': files_of('1.1')}
    )
    tagged(world, '1.0', '1.1')
    world.latest = [None, 'v1.0', 'v1.0']
    assert backfill.main(['spam', '--checkout', str(tmp_path)]) == 2
    message: str = (
        'after creating v1.1 GitHub reports Latest v1.0, expected v1.1, '
        'stopping'
    )
    captured = capsys.readouterr()
    assert message in captured.err
    assert '| 1.1 | v1.1 | exists | create | pypi | skipped |' in (
        captured.out
    )
    assert f'Stopped: {message}' in captured.out
    assert 'Latest before: none, after: v1.0, target: none' in captured.out
    assert captured.out.rstrip().endswith('CHANGED')
    assert [created[0] for created in world.created] == ['v1.1']
    assert not world.signed
    assert not world.marked


def test_an_unreadable_latest_mid_run_stops_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.0': files_of('1.0')})
    tagged(world, '1.0')
    calls: list[int] = []

    def latest(repo: str) -> github.Release | None:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError('gh is down')
        return None

    monkeypatch.setattr(github, 'latest_release', latest)
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert 'could not read Latest after creating v1.0' in report.stopped
    assert backfill._exit_status(report) == 2
    assert not world.signed


def test_latest_before_unchanged_is_ok_and_never_marked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with(
        {'1.0': files_of('1.0'), '1.1': files_of('1.1')}
    )
    tagged(world, '1.0', '1.1')
    # Only two reads: no mid-run check when a Latest existed before.
    world.latest = ['v1.0', 'v1.0']
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert [created[0] for created in world.created] == ['v1.0', 'v1.1']
    assert world.marked == []
    assert report.latest_target == 'v1.0'
    text: str = backfill.to_markdown(report)
    assert 'Latest before: v1.0, after: v1.0, target: v1.0' in text
    assert text.rstrip().endswith('OK')


def test_a_release_workflow_at_the_tag_commit_refuses_the_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.0': files_of('1.0')})
    tagged(world, '1.0')
    world.release_workflows = ['.github/workflows/publish.yml']
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert world.release_shas == [SHA]
    assert not world.created
    assert not world.signed
    _, outcome, note = report.rows[0]
    assert outcome is None
    assert note == 'release would fire publish.yml at the tagged commit'
    assert backfill._exit_status(report) == 1
    # Disabled on GitHub, the same workflow no longer blocks the release.
    world.disabled = {'.github/workflows/publish.yml'}
    world.latest = ['v1.0', 'v1.0']
    allowed: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert [created[0] for created in world.created] == ['v1.0']
    assert allowed.rows[0][1] is not None


def test_an_existing_release_skips_the_release_workflow_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.0': files_of('1.0')})
    tagged(world, '1.0')
    world.releases = [release('v1.0')]
    world.release_workflows = ['.github/workflows/publish.yml']
    backfill.run_backfill('spam', options(tmp_path))
    assert world.release_shas == []
    assert len(world.signed) == 1


def test_a_release_workflow_at_the_anchor_refuses_the_untagged_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.0': files_of('1.0')})
    world.match_sha = None
    world.release_workflows = ['.github/workflows/publish.yml']
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert not world.created
    assert world.release_shas == [ROOT]
    assert report.rows[0][2] == (
        'release would fire publish.yml at the tagged commit'
    )


def test_the_untagged_release_gets_the_multi_version_footer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.0': files_of('1.0')})
    world.match_sha = None
    backfill.run_backfill('spam', options(tmp_path))
    _, _, sign_options = world.signed[0]
    assert sign_options.multi_version


def test_tags_are_signed_with_the_configured_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.2': files_of('1.2')})
    backfill.run_backfill('spam', options(tmp_path))
    assert world.tag_keys == [signing.DEFAULT_GPG_KEY]
    world = install(monkeypatch, World())
    world.project = project_with({'1.2': files_of('1.2')})
    keyed: backfill.BackfillOptions = dataclasses.replace(
        options(tmp_path), gpg_key='SPAM'
    )
    backfill.run_backfill('spam', keyed)
    assert world.tag_keys == ['SPAM']


def signed_assets(*names: str) -> list[github.Asset]:
    return [github.Asset(n, 1, f'https://assets.example/{n}') for n in names]


WHEEL: str = 'spam-1.0-py3-none-any.whl'


def test_an_already_signed_release_is_not_rebuilt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.0': []})
    tagged(world, '1.0')
    names: list[str] = [
        WHEEL,
        f'{WHEEL}.asc',
        f'{WHEEL}.sigstore.json',
        'SHA256SUMS',
        'SHA256SUMS.asc',
    ]
    world.releases = [
        dataclasses.replace(
            release('v1.0', body=f'notes\n\n{sign.FOOTER_MARKER}\n'),
            assets=signed_assets(*names),
        )
    ]
    world.assets['SHA256SUMS'] = (
        f'# rebuilt from tag v1.0\n{"0" * 64}  {WHEEL}\n'
    ).encode()
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert world.rebuilt == []
    assert not world.signed
    _, outcome, note = report.rows[0]
    assert outcome is not None
    assert outcome.status == 'already signed'
    assert sorted(outcome.kept) == sorted(names)
    assert note == backfill.REBUILD_SKIPPED_NOTE
    assert backfill._exit_status(report) == 0


@pytest.mark.parametrize('missing', ['SHA256SUMS', 'SHA256SUMS.asc'])
def test_a_release_without_a_signed_sums_list_is_still_rebuilt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, missing: str
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'1.0': []})
    tagged(world, '1.0')
    names: list[str] = [
        WHEEL,
        f'{WHEEL}.asc',
        f'{WHEEL}.sigstore.json',
        'SHA256SUMS',
        'SHA256SUMS.asc',
    ]
    world.releases = [
        dataclasses.replace(
            release('v1.0'),
            assets=signed_assets(*(n for n in names if n != missing)),
        )
    ]
    world.assets['SHA256SUMS'] = f'{"0" * 64}  {WHEEL}\n'.encode()
    backfill.run_backfill('spam', options(tmp_path))
    assert world.rebuilt == ['v1.0']


class Signer:
    """gpg, sigstore and upload fakes for a run with the real sign_release."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.sigstore_calls: list[list[pathlib.Path]] = []
        self.uploads: list[tuple[str, list[str]]] = []
        self.edits: list[str] = []
        self.bodies: list[str] = []
        self.sigstore_error: str = ''
        monkeypatch.setattr(pypi, 'download', self.download)
        monkeypatch.setattr(signing, 'gpg_sign', self.gpg_sign)
        monkeypatch.setattr(signing, 'sigstore_sign', self.sigstore_sign)
        monkeypatch.setattr(github, 'upload_assets', self.upload_assets)
        monkeypatch.setattr(github, 'edit_notes', self.edit_notes)

    @staticmethod
    def download(file: pypi.PypiFile, into: pathlib.Path) -> pathlib.Path:
        into.mkdir(parents=True, exist_ok=True)
        target: pathlib.Path = into / file.filename
        target.write_bytes(file.filename.encode())
        return target

    @staticmethod
    def gpg_sign(path: pathlib.Path, key: str) -> pathlib.Path:
        out: pathlib.Path = path.with_name(path.name + '.asc')
        out.write_text('signature')
        return out

    def sigstore_sign(self, paths: list[pathlib.Path]) -> list[pathlib.Path]:
        self.sigstore_calls.append(list(paths))
        if self.sigstore_error:
            raise signing.SigningError(self.sigstore_error)
        bundles: list[pathlib.Path] = [signing.bundle_path(p) for p in paths]
        for bundle in bundles:
            bundle.write_text('bundle')
        return bundles

    def upload_assets(
        self,
        repo: str,
        tag: str,
        paths: list[pathlib.Path],
        clobber: bool = False,
    ) -> None:
        self.uploads.append((tag, [p.name for p in paths]))

    def edit_notes(self, repo: str, tag: str, body: str) -> None:
        self.edits.append(tag)
        self.bodies.append(body)

    def bundle_uploads(self) -> list[tuple[str, list[str]]]:
        return [
            (tag, names)
            for tag, names in self.uploads
            if all(n.endswith('.sigstore.json') for n in names)
        ]


def test_one_sigstore_invocation_for_the_whole_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World(), fake_sign=False)
    signer: Signer = Signer(monkeypatch)
    world.project = project_with(
        {v: files_of(v) for v in ('1.0', '1.1', '1.2')}
    )
    tagged(world, '1.0', '1.1', '1.2')
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert len(signer.sigstore_calls) == 1
    assert len(signer.sigstore_calls[0]) == 6
    assert signer.bundle_uploads() == [
        (
            f'v{v}',
            [
                f'spam-{v}.tar.gz.sigstore.json',
                f'spam-{v}-py3-none-any.whl.sigstore.json',
            ],
        )
        for v in ('1.0', '1.1', '1.2')
    ]
    # Every gpg upload comes before the first bundle upload.
    first_bundle: int = signer.uploads.index(signer.bundle_uploads()[0])
    assert all(
        not any(n.endswith('.sigstore.json') for n in names)
        for _, names in signer.uploads[:first_bundle]
    )
    # The footer promises bundles, so it waits for them.
    assert signer.edits == ['v1.0', 'v1.1', 'v1.2']
    statuses: list[str] = [
        outcome.status for _, outcome, _ in report.rows if outcome
    ]
    assert statuses == ['signed', 'signed', 'signed']
    assert backfill._exit_status(report) == 0


def test_a_failed_sigstore_run_fails_every_waiting_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World(), fake_sign=False)
    signer: Signer = Signer(monkeypatch)
    signer.sigstore_error = 'login cancelled'
    world.project = project_with({v: files_of(v) for v in ('1.0', '1.1')})
    tagged(world, '1.0', '1.1')
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert signer.bundle_uploads() == []
    assert signer.edits == []
    for _, outcome, _ in report.rows:
        assert outcome is not None
        assert outcome.status == 'failed'
        assert outcome.reason == 'sigstore failed: login cancelled'
    assert backfill._exit_status(report) == 1


def test_a_sigstore_timeout_fails_the_waiting_releases_not_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World(), fake_sign=False)
    Signer(monkeypatch)
    message: str = signing.SIGSTORE_TIMEOUT_MESSAGE.format(seconds=900)

    def timed_out(paths: list[pathlib.Path]) -> list[pathlib.Path]:
        raise RuntimeError(message)

    monkeypatch.setattr(signing, 'sigstore_sign', timed_out)
    world.project = project_with({'1.0': files_of('1.0')})
    tagged(world, '1.0')
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    _, outcome, _ = report.rows[0]
    assert outcome is not None
    assert outcome.status == 'failed'
    assert outcome.reason == f'sigstore failed: {message}'
    assert backfill._exit_status(report) == 1


SDIST: str = 'spam-1.0.tar.gz'
REBUILT_COMMENT: str = (
    '# rebuilt from tag v1.0 on 2026-11-05, not the PyPI bytes'
)


class Verifier:
    """Stand-in for signing.gpg_verify_key with a fixed verdict."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, good: bool) -> None:
        self.good: bool = good
        self.calls: list[tuple[str, str, str]] = []
        monkeypatch.setattr(signing, 'gpg_verify_key', self)

    def __call__(
        self, path: pathlib.Path, signature: pathlib.Path, key: str
    ) -> bool:
        self.calls.append((path.name, signature.name, key))
        return self.good


def interrupted(world: World, body: str = 'notes') -> list[str]:
    """A rebuilt v1.0 that a run left without bundles, returning its assets.

    The release has both dists with their ``.asc``, SHA256SUMS and its
    signature, and no footer unless ``body`` carries one.
    """
    world.project = project_with({'1.0': []})
    tagged(world, '1.0')
    world.assets = {SDIST: b'sdist bytes', WHEEL: b'wheel bytes'}
    lines: list[str] = []
    for name in (SDIST, WHEEL):
        digest: str = hashlib.sha256(world.assets[name]).hexdigest()
        lines.extend([REBUILT_COMMENT, f'{digest}  {name}'])
    world.assets['SHA256SUMS'] = ('\n'.join(lines) + '\n').encode()
    world.assets['SHA256SUMS.asc'] = b'signature of the list'
    world.assets[f'{SDIST}.asc'] = b'signature'
    world.assets[f'{WHEEL}.asc'] = b'signature'
    names: list[str] = sorted(world.assets)
    world.releases = [
        dataclasses.replace(
            release('v1.0', body=body), assets=signed_assets(*names)
        )
    ]
    return names


def test_an_interrupted_rebuild_resumes_from_the_release_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World(), fake_sign=False)
    signer: Signer = Signer(monkeypatch)
    verifier: Verifier = Verifier(monkeypatch, good=True)
    interrupted(world)
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert world.rebuilt == []
    assert {'SHA256SUMS', 'SHA256SUMS.asc', SDIST, WHEEL} <= set(
        world.asset_downloads
    )
    assert verifier.calls == [
        ('SHA256SUMS', 'SHA256SUMS.asc', signing.DEFAULT_GPG_KEY)
    ]
    assert [[p.name for p in call] for call in signer.sigstore_calls] == [
        [SDIST, WHEEL]
    ]
    # The dists, their .asc and the signed list stay as they are.
    assert signer.uploads == [
        ('v1.0', [f'{SDIST}.sigstore.json', f'{WHEEL}.sigstore.json'])
    ]
    assert signer.edits == ['v1.0']
    assert sign.FOOTER_MARKER in signer.bodies[0]
    assert f'Rebuilt files:\n\n- {SDIST}\n- {WHEEL}\n' in signer.bodies[0]
    plan, outcome, note = report.rows[0]
    assert plan.files_source == 'rebuild'
    assert outcome is not None
    assert outcome.status == 'signed'
    assert note == backfill.RESUMED_NOTE
    assert backfill._exit_status(report) == 0


def test_a_resumed_release_with_bundles_only_gets_its_footer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World(), fake_sign=False)
    signer: Signer = Signer(monkeypatch)
    Verifier(monkeypatch, good=True)
    interrupted(world)
    for name in (SDIST, WHEEL):
        world.assets[f'{name}.sigstore.json'] = b'bundle'
    world.releases[0].assets = signed_assets(*sorted(world.assets))
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert world.rebuilt == []
    assert signer.sigstore_calls == []
    assert signer.uploads == []
    assert signer.edits == ['v1.0']
    _, outcome, _ = report.rows[0]
    assert outcome is not None
    assert outcome.status == 'already signed'


def _assert_failed_untouched(
    world: World,
    signer: Signer,
    report: backfill.BackfillReport,
    reason: str,
) -> None:
    assert world.rebuilt == []
    assert signer.uploads == []
    assert signer.edits == []
    assert signer.sigstore_calls == []
    _, outcome, _ = report.rows[0]
    assert outcome is not None
    assert outcome.status == 'failed'
    assert outcome.reason == reason
    assert backfill._exit_status(report) == 1


def test_a_release_file_that_differs_from_the_list_fails_the_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World(), fake_sign=False)
    signer: Signer = Signer(monkeypatch)
    Verifier(monkeypatch, good=True)
    interrupted(world)
    world.assets[WHEEL] = b'tampered'
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    actual: str = hashlib.sha256(b'tampered').hexdigest()
    listed: str = hashlib.sha256(b'wheel bytes').hexdigest()
    _assert_failed_untouched(
        world,
        signer,
        report,
        f'{WHEEL} on the release has sha256 {actual}, SHA256SUMS says '
        f'{listed}',
    )


def test_a_bad_sums_signature_fails_the_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World(), fake_sign=False)
    signer: Signer = Signer(monkeypatch)
    Verifier(monkeypatch, good=False)
    interrupted(world)
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    _assert_failed_untouched(
        world,
        signer,
        report,
        f'SHA256SUMS signature not valid for {signing.DEFAULT_GPG_KEY}',
    )
    # No dist is fetched from a list nobody vouches for.
    assert SDIST not in world.asset_downloads
    assert WHEEL not in world.asset_downloads


@pytest.mark.parametrize(
    ('listing', 'reason'),
    [
        (
            f'{"0" * 64}  {WHEEL}\n',
            f'SHA256SUMS does not mark as rebuilt: {WHEEL}',
        ),
        (
            f'{REBUILT_COMMENT}\n{"0" * 64}  spam-1.0-cp39.whl\n',
            'spam-1.0-cp39.whl is in SHA256SUMS but not on the release',
        ),
        (
            f'{REBUILT_COMMENT}\n{"0" * 64}  ../{WHEEL}\n',
            f"SHA256SUMS lists a bad file name '../{WHEEL}'",
        ),
        (
            f'{REBUILT_COMMENT}\n{"0" * 64}  {WHEEL}.asc\n',
            f"SHA256SUMS lists a bad file name '{WHEEL}.asc'",
        ),
        ('# only a comment\n', 'SHA256SUMS lists no files'),
    ],
)
def test_a_sums_list_that_cannot_be_resumed_fails_the_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    listing: str,
    reason: str,
) -> None:
    world: World = install(monkeypatch, World(), fake_sign=False)
    signer: Signer = Signer(monkeypatch)
    Verifier(monkeypatch, good=True)
    interrupted(world)
    world.assets['SHA256SUMS'] = listing.encode()
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    _assert_failed_untouched(world, signer, report, reason)


def test_already_signed_needs_the_footer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    interrupted(world)
    for name in (SDIST, WHEEL):
        world.assets[f'{name}.sigstore.json'] = b'bundle'
    names: list[str] = sorted(world.assets)
    plan: backfill.Plan = backfill.plan_versions(
        world.project, world.tags, world.releases
    )[0]
    opts: backfill.BackfillOptions = options(tmp_path)

    def run_with(body: str) -> backfill._Run:
        signed: github.Release = dataclasses.replace(
            release('v1.0', body=body), assets=signed_assets(*names)
        )
        return backfill._Run(
            package='spam',
            options=opts,
            project=world.project,
            tags=world.tags,
            releases={'v1.0': signed},
            workdir=tmp_path / 'work',
            sign_options=backfill._sign_options('spam', opts),
        )

    assert backfill._already_signed(run_with('notes'), plan) is None
    footed: sign.SignOutcome | None = backfill._already_signed(
        run_with(f'notes\n\n{sign.FOOTER_MARKER}\n'), plan
    )
    assert footed is not None
    assert footed.status == 'already signed'


def test_dry_run_says_a_resumable_release_is_checked_not_rebuilt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world: World = install(monkeypatch, World())
    interrupted(world)
    backfill.run_backfill('spam', options(tmp_path, dry_run=True))
    out: str = capsys.readouterr().out
    assert 'would check the files on release v1.0 against its SHA256SUMS' in (
        out
    )
    assert 'would rebuild' not in out


def test_chunks_cap_the_size_and_keep_names_apart() -> None:
    paths: list[pathlib.Path] = [
        pathlib.Path(f'v{n}') / f'f{n}.whl' for n in range(250)
    ]
    sizes: list[int] = [len(c) for c in backfill._chunks(paths, 100)]
    assert sizes == [100, 100, 50]
    clash: list[pathlib.Path] = [
        pathlib.Path('a') / 'x.whl',
        pathlib.Path('b') / 'x.whl',
        pathlib.Path('a') / 'y.whl',
    ]
    assert backfill._chunks(clash, 100) == [
        [clash[0], clash[2]],
        [clash[1]],
    ]


def run_empty_repo(
    monkeypatch: pytest.MonkeyPatch,
    latest: list[str | None],
    release_tags: bool = True,
) -> World:
    """A repository without releases and the versions 0.1 to 1.0."""
    world: World = install(monkeypatch, World())
    world.project = project_with(
        {v: files_of(v) for v in ('0.1', '0.2', '0.9rc1', '1.0')}
    )
    if release_tags:
        tagged(world, '0.1', '0.2', '0.9rc1', '1.0')
    world.latest = latest
    return world


def test_an_empty_repository_releases_the_target_first(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Before the run, after the first release, before marking, after the run.
    world: World = run_empty_repo(monkeypatch, [None, 'v1.0', 'v1.0', 'v1.0'])
    assert backfill.main(['spam', '--checkout', str(tmp_path)]) == 0
    assert [created[0] for created in world.created] == [
        'v1.0',
        'v0.1',
        'v0.2',
        'v0.9rc1',
    ]
    assert [signed[0].tag for signed in world.signed] == [
        'v1.0',
        'v0.1',
        'v0.2',
        'v0.9rc1',
    ]
    assert world.marked == ['v1.0']
    assert world.events[:3] == ['create v1.0', 'mark v1.0', 'create v0.1']
    assert world.latest == []
    printed: str = capsys.readouterr().out
    assert 'Latest before: none, after: v1.0, target: v1.0' in printed
    assert printed.rstrip().endswith('OK')


def test_a_target_without_a_tag_is_tagged_and_accepted_as_latest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = run_empty_repo(
        monkeypatch, [None, 'v1.0', 'v1.0', 'v1.0'], release_tags=False
    )
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert world.new_tags[0][0] == 'v1.0'
    assert world.created[0][0] == 'v1.0'
    assert not report.stopped
    assert report.latest_target == 'v1.0'
    assert world.marked == ['v1.0']


def test_latest_on_an_older_release_after_the_first_stops_the_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world: World = run_empty_repo(monkeypatch, [None, 'v0.1', 'v0.1'])
    assert backfill.main(['spam', '--checkout', str(tmp_path)]) == 2
    message: str = (
        'after creating v1.0 GitHub reports Latest v0.1, expected v1.0, '
        'stopping'
    )
    captured = capsys.readouterr()
    assert message in captured.err
    assert f'Stopped: {message}' in captured.out
    assert [created[0] for created in world.created] == ['v1.0']
    assert not world.marked


def test_no_latest_after_the_first_release_stops_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = run_empty_repo(monkeypatch, [None] * 7)
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert report.stopped == (
        'after creating v1.0 GitHub reports Latest none, expected v1.0, '
        'stopping'
    )
    assert backfill._exit_status(report) == 2
    assert [created[0] for created in world.created] == ['v1.0']
    # Five reads after the first release, four sleeps between them.
    assert world.sleeps == [2.0] * 4
    assert world.latest == []


def test_a_latest_that_lags_behind_the_first_release_is_waited_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # Before the run, three reads after the first release, before marking
    # and after the run.
    world: World = run_empty_repo(
        monkeypatch, [None, None, None, 'v1.0', 'v1.0', 'v1.0']
    )
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert not report.stopped
    assert world.sleeps == [2.0, 2.0]
    assert [created[0] for created in world.created] == [
        'v1.0',
        'v0.1',
        'v0.2',
        'v0.9rc1',
    ]
    assert world.marked == ['v1.0']
    assert world.latest == []
    assert backfill._exit_status(report) == 0


def test_a_wrong_latest_after_the_first_release_is_not_waited_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = run_empty_repo(monkeypatch, [None, 'v0.1', 'v0.1'])
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert report.stopped == (
        'after creating v1.0 GitHub reports Latest v0.1, expected v1.0, '
        'stopping'
    )
    assert world.sleeps == []
    assert [created[0] for created in world.created] == ['v1.0']
    assert backfill._exit_status(report) == 2


def test_an_unreadable_latest_while_waiting_stops_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = run_empty_repo(monkeypatch, [])
    answers: list[str | None] = [None, None]

    def latest(repo: str) -> github.Release | None:
        if not answers:
            raise RuntimeError('gh is down')
        tag: str | None = answers.pop(0)
        return None if tag is None else release(tag)

    monkeypatch.setattr(github, 'latest_release', latest)
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert report.stopped.startswith('could not read Latest after creating')
    assert world.sleeps == [2.0]
    assert [created[0] for created in world.created] == ['v1.0']


def test_a_rerun_fills_the_empty_target_release_a_stopped_run_left(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # The stopped run created v1.0.1 and nothing else, GitHub then marked
    # it Latest.
    world: World = install(monkeypatch, World(), fake_sign=False)
    signer: Signer = Signer(monkeypatch)
    world.project = project_with({'1.0.1': files_of('1.0.1')})
    tagged(world, '1.0.1')
    world.releases = [release('v1.0.1', body='Release 1.0.1.')]
    world.latest = ['v1.0.1', 'v1.0.1']
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert not world.created
    assert not world.marked
    sdist: str = 'spam-1.0.1.tar.gz'
    wheel: str = 'spam-1.0.1-py3-none-any.whl'
    uploaded: set[str] = {
        name for _, names in signer.uploads for name in names
    }
    assert {tag for tag, _ in signer.uploads} == {'v1.0.1'}
    assert uploaded == {
        sdist,
        wheel,
        f'{sdist}.asc',
        f'{wheel}.asc',
        f'{sdist}.sigstore.json',
        f'{wheel}.sigstore.json',
        'SHA256SUMS',
        'SHA256SUMS.asc',
    }
    assert len(signer.sigstore_calls) == 1
    assert signer.edits == ['v1.0.1']
    assert sign.FOOTER_MARKER in signer.bodies[0]
    assert signer.bodies[0].startswith('Release 1.0.1.')
    _, outcome, _ = report.rows[0]
    assert outcome is not None
    assert outcome.status == 'signed'
    assert backfill._exit_status(report) == 0


def test_a_rerun_without_latest_yet_fills_the_empty_target_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # GitHub still reports no Latest: the existing release is the target
    # and is marked at the end, nothing new is created.
    world: World = install(monkeypatch, World(), fake_sign=False)
    signer: Signer = Signer(monkeypatch)
    world.project = project_with({'1.0.1': files_of('1.0.1')})
    tagged(world, '1.0.1')
    world.releases = [release('v1.0.1')]
    # Before the run, before marking, after the run.
    world.latest = [None, None, 'v1.0.1']
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert not world.created
    assert world.marked == ['v1.0.1']
    assert len(signer.sigstore_calls) == 1
    assert signer.edits == ['v1.0.1']
    assert report.latest_target == 'v1.0.1'
    assert backfill._exit_status(report) == 0


def test_a_target_that_fails_before_a_release_falls_through(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world: World = run_empty_repo(
        monkeypatch,
        [None, 'v0.2', 'v0.2', 'v0.2'],
        release_tags=False,
    )

    def download(file: pypi.PypiFile, into: pathlib.Path) -> pathlib.Path:
        if file.filename == 'spam-1.0.tar.gz':
            raise pypi.IntegrityError('sha256 mismatch')
        return into / file.filename

    monkeypatch.setattr(pypi, 'download', download)
    assert backfill.main(['spam', '--checkout', str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert '| 1.0 |' in captured.out
    assert 'tag creation failed: sha256 mismatch' in captured.out
    assert [created[0] for created in world.created] == [
        'v0.2',
        'v0.1',
        'v0.9rc1',
    ]
    assert 'target: v0.2' in captured.out


def test_a_target_failing_after_its_release_stops_the_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world: World = run_empty_repo(monkeypatch, [None, 'v1.0', 'v1.0'])

    def upload_raises(
        release: github.Release,
        files: Files,
        workdir: pathlib.Path,
        options: sign.SignOptions,
    ) -> sign.SignOutcome:
        raise RuntimeError('upload failed')

    monkeypatch.setattr(sign, 'sign_release', upload_raises)
    assert backfill.main(['spam', '--checkout', str(tmp_path)]) == 2
    reason: str = 'target 1.0 failed after its release was created, stopping'
    captured = capsys.readouterr()
    assert reason in captured.err
    assert f'Stopped: {reason}' in captured.out
    assert [created[0] for created in world.created] == ['v1.0']
    assert not world.new_tags
    # The pin ran before the target failed.
    assert world.marked == ['v1.0']
    lines: list[str] = [
        line for line in captured.out.splitlines() if line.startswith('| ')
    ]
    # Header, separator, the target, then the three skipped plans.
    assert len(lines) == 6
    assert 'release failed: upload failed' in lines[2]
    for line, version in zip(lines[3:], ('0.1', '0.2', '0.9rc1'), strict=True):
        assert line.startswith(f'| {version} |')
        assert '| skipped |' in line
        assert reason in line


def test_a_target_whose_release_call_raises_stops_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = run_empty_repo(monkeypatch, [None, None])

    def create_raises(*args: object) -> github.Release:
        world.created.append((str(args[1]), '', '', False))
        raise common.FetchError('gateway timeout')

    monkeypatch.setattr(github, 'create_release', create_raises)
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert report.stopped.startswith('target 1.0 failed after its release')
    assert backfill._exit_status(report) == 2
    assert [created[0] for created in world.created] == ['v1.0']


def test_untagged_candidates_fall_through_and_join_the_anchor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sphinx-PyPI-upload-2: 0.2.2 is on PyPI only and matches no commit."""
    world: World = install(monkeypatch, World())
    world.project = project_with({'0.2.2': files_of('0.2.2')})
    tagged(world, '0.1', '0.1.1', '0.2', '0.2.1')
    world.match_sha = None
    # Before the run, after 0.2.1, before marking, after the run.
    world.latest = [None, 'v0.2.1', 'v0.2.1', 'v0.2.1']
    assert backfill.main(['spam', '--checkout', str(tmp_path)]) == 0
    expected: list[str] = [
        'v0.2.1',
        'v0.1',
        'v0.1.1',
        'v0.2',
        backfill.UNTAGGED_TAG,
    ]
    assert [created[0] for created in world.created] == expected
    assert [signed[0].tag for signed in world.signed] == expected
    assert world.rebuilt == ['v0.2.1', 'v0.1', 'v0.1.1', 'v0.2']
    assert world.downloads == ['spam-0.2.2.tar.gz']
    assert world.marked == ['v0.2.1']
    assert world.events[:3] == [
        'create v0.2.1',
        'mark v0.2.1',
        'create v0.1',
    ]
    assert world.latest == []
    printed: str = capsys.readouterr().out
    rows: list[str] = [
        line for line in printed.splitlines() if line.startswith('| 0')
    ]
    assert [row.split(' | ')[0] for row in rows] == [
        '| 0.2.1',
        '| 0.1',
        '| 0.1.1',
        '| 0.2',
        '| 0.2.2',
    ]
    assert 'Latest before: none, after: v0.2.1, target: v0.2.1' in printed
    assert printed.rstrip().endswith('OK')


def test_a_candidate_is_never_processed_twice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with({'0.2.2': files_of('0.2.2')})
    tagged(world, '0.1', '0.2.1')
    world.match_sha = None
    world.latest = [None, 'v0.2.1', 'v0.2.1', 'v0.2.1']
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    versions_seen: list[str] = [plan.version for plan, _, _ in report.rows]
    assert versions_seen == ['0.2.1', '0.1', '0.2.2']
    assert len(world.created) == 3


def test_no_candidate_with_a_commit_makes_the_anchor_the_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    anchor: str = backfill.UNTAGGED_TAG
    world: World = run_empty_repo(
        monkeypatch, [None, anchor, anchor, anchor], release_tags=False
    )
    world.match_sha = None
    assert backfill.main(['spam', '--checkout', str(tmp_path)]) == 0
    assert [created[0] for created in world.created] == [anchor]
    assert world.pushed == [anchor]
    assert world.marked == [anchor]
    printed: str = capsys.readouterr().out
    assert f'Latest before: none, after: {anchor}, target: {anchor}' in printed
    assert printed.rstrip().endswith('OK')


def test_every_candidate_refused_by_a_workflow_creates_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = run_empty_repo(monkeypatch, [None, None, None])
    world.release_workflows = ['.github/workflows/publish.yml']
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert not report.stopped
    assert not world.created
    assert report.latest_target is None
    assert backfill._exit_status(report) == 1
    assert [plan.version for plan, _, _ in report.rows] == [
        '1.0',
        '0.2',
        '0.1',
        '0.9rc1',
    ]
    assert report.rows[0][2] == (
        'release would fire publish.yml at the tagged commit'
    )


def test_an_existing_target_release_is_marked_when_nothing_is_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with(
        {'0.9': files_of('0.9'), '1.0': files_of('1.0')}
    )
    tagged(world, '0.9', '1.0')
    world.releases = [release('v0.9'), release('v1.0')]
    # Before the run, before marking, after the run.
    world.latest = [None, None, 'v1.0']
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert not world.created
    assert world.marked == ['v1.0']
    assert report.latest_target == 'v1.0'
    assert not report.stopped


def test_a_repository_with_latest_keeps_the_oldest_first_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    world: World = install(monkeypatch, World())
    world.project = project_with(
        {v: files_of(v) for v in ('0.9', '1.0', '1.1')}
    )
    tagged(world, '0.9', '1.0', '1.1')
    # No mid-run read and no marking: Latest existed before the run.
    world.latest = ['v1.0', 'v1.0']
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(tmp_path)
    )
    assert [created[0] for created in world.created] == [
        'v0.9',
        'v1.0',
        'v1.1',
    ]
    assert [plan.version for plan, _, _ in report.rows] == [
        '0.9',
        '1.0',
        '1.1',
    ]
    assert report.latest_target == 'v1.0'
    assert not world.marked


def test_dry_run_prints_the_target_and_the_order_first(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world: World = run_empty_repo(monkeypatch, [None, None])
    backfill.run_backfill('spam', options(tmp_path, dry_run=True))
    lines: list[str] = capsys.readouterr().out.splitlines()
    assert lines[0] == 'target candidates: 1.0, 0.2, 0.1'
    assert lines[1] == 'order: 1.0, 0.1, 0.2, 0.9rc1'
    assert not world.created
    assert world.latest == []


def test_dry_run_names_the_existing_latest_as_the_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world: World = install(monkeypatch, World(), fake_sign=False)
    world.project = project_with(
        {'0.9': files_of('0.9'), '1.0': files_of('1.0')}
    )
    tagged(world, '0.9', '1.0')
    backfill.run_backfill('spam', options(tmp_path, dry_run=True))
    lines: list[str] = capsys.readouterr().out.splitlines()
    assert lines[0] == 'target: v1.0 (the existing Latest)'
    assert lines[1] == 'order: 0.9, 1.0'
