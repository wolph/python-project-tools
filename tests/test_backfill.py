"""Tests for ppt backfill."""

from __future__ import annotations

import dataclasses
import datetime as dt
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
        self.sign_status: str = 'signed'
        self.rec: Recorder = Recorder({})


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
    return release(tag)


def _rebuild(
    world: World, checkout: pathlib.Path, tag: str, out: pathlib.Path
) -> list[pathlib.Path]:
    world.rebuilt.append(tag)
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


def install(monkeypatch: pytest.MonkeyPatch, world: World) -> World:
    world.rec = Recorder(
        {
            'git show': completed('', 128),
            'git rev-list': completed(ROOT + '\n'),
            'git rev-parse': completed('', 1),
        }
    )
    monkeypatch.setattr(common, 'run', world.rec)
    monkeypatch.setattr(pypi, 'project', lambda package: world.project)

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
    monkeypatch.setattr(
        match_tag,
        'create_tag',
        lambda checkout, tag, sha, message: world.new_tags.append(
            (tag, sha, message)
        ),
    )
    monkeypatch.setattr(
        match_tag, 'push_tag', lambda checkout, tag: world.pushed.append(tag)
    )
    return world


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
    monkeypatch.setattr(
        github, 'release_triggered_workflows', lambda repo: ['release.yml']
    )
    assert backfill.preflight('WoLpH/spam') == [
        'release.yml is enabled and triggers on release events'
    ]


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
    world: World = install(monkeypatch, World())
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
    ):
        assert phrase in printed
    assert not world.created
    assert not world.new_tags
    assert not world.pushed
    assert not world.downloads
    assert not world.rebuilt
    assert world.signed
    assert all(opts.dry_run for _, _, opts in world.signed)
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
