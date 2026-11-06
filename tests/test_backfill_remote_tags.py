"""Backfill against a real checkout whose local tags differ from GitHub's.

GitHub, PyPI and signing stay faked, git runs for real on the scratch
repository and its bare origin, which stands in for GitHub.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from python_project_tools import (
    backfill,
    common,
    github,
    match_tag,
    rebuild,
    remote_tags,
    sign,
)
from tests.conftest import ScratchRepo, git
from tests.fakes import completed
from tests.test_backfill import (
    Files,
    World,
    _sign_release,
    files_of,
    install,
    options,
    project_with,
)

# Captured at import, before any test replaces them.
REAL_RUN = common.run
REAL_REBUILD = rebuild.rebuild
REAL_COMMIT = remote_tags.commit
REAL_LOCAL_OBJECT = remote_tags.local_object
REAL_CREATE_TAG = match_tag.create_tag
REAL_PUSH_TAG = match_tag.push_tag
WHEEL: str = 'spam-1.0-py3-none-any.whl'


class RealGit:
    """Real git, a fake ``uv build`` that packs the exported version file.

    The wheel it writes holds the bytes of ``spam/__init__.py`` of the
    tree it was run in, so the file names the commit that was exported.
    """

    def __init__(self) -> None:
        self.build_envs: list[dict[str, str] | None] = []

    def __call__(
        self,
        args: list[str],
        check: bool = False,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        capture: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if args[:2] != ['uv', 'build']:
            return REAL_RUN(args, check, cwd, env, capture, timeout)
        assert cwd is not None
        self.build_envs.append(env)
        out: pathlib.Path = pathlib.Path(args[args.index('--out-dir') + 1])
        (out / WHEEL).write_bytes((cwd / 'spam' / '__init__.py').read_bytes())
        return completed()


def install_real_git(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> tuple[RealGit, dict[str, bytes]]:
    """The fakes of ``install`` with git, tags and rebuild made real.

    Returns the runner and the bytes of every file handed to signing,
    read while the files still exist.
    """
    install(monkeypatch, world)
    runner: RealGit = RealGit()
    monkeypatch.setattr(common, 'run', runner)
    monkeypatch.setattr(rebuild, 'rebuild', REAL_REBUILD)
    monkeypatch.setattr(remote_tags, 'commit', REAL_COMMIT)
    monkeypatch.setattr(remote_tags, 'local_object', REAL_LOCAL_OBJECT)
    monkeypatch.setattr(match_tag, 'create_tag', REAL_CREATE_TAG)
    monkeypatch.setattr(match_tag, 'push_tag', REAL_PUSH_TAG)
    signed: dict[str, bytes] = {}

    def sign_release(
        release: github.Release,
        files: Files,
        workdir: pathlib.Path,
        sign_options: sign.SignOptions,
    ) -> sign.SignOutcome:
        for file in files:
            if isinstance(file, pathlib.Path):
                signed[file.name] = file.read_bytes()
        return _sign_release(world, release, files, workdir, sign_options)

    monkeypatch.setattr(sign, 'sign_release', sign_release)
    return runner, signed


def diverge(
    repo: ScratchRepo, name: str, github_at: str, local_at: str
) -> None:
    """Tag ``github_at`` on origin and ``local_at`` locally, same name."""
    git(repo.path, 'tag', name, github_at)
    git(repo.path, 'push', '-q', 'origin', name)
    git(repo.path, 'tag', '-d', name)
    git(repo.path, 'tag', name, local_at)


def all_refs(path: pathlib.Path) -> str:
    """Every ref of a repository with its object, for before and after."""
    return git(path, 'for-each-ref', '--format=%(objectname) %(refname)')


def test_a_diverging_local_tag_leaves_backfill_on_github_tag(
    scratch_repo: ScratchRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    path: pathlib.Path = scratch_repo.path
    first, second, third = scratch_repo.shas
    diverge(scratch_repo, 'v1.0', first, second)
    diverge(scratch_repo, 'v0.9', first, third)
    # A tag only the checkout has, and the settings that would make a
    # pruning fetch delete it.
    git(path, 'tag', 'v0.1', first)
    git(path, 'config', 'fetch.prune', 'true')
    git(path, 'config', 'fetch.pruneTags', 'true')
    plain: subprocess.CompletedProcess[str] = subprocess.run(
        ['git', 'fetch', 'origin', '--tags', '--dry-run'],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert plain.returncode != 0
    assert 'would clobber existing tag' in plain.stderr
    local_before: str = all_refs(path)
    origin_before: str = all_refs(scratch_repo.origin)
    world: World = World()
    runner, signed = install_real_git(monkeypatch, world)
    world.project = project_with({'1.0': []})
    # v0.9 stays out of the API listing so it plans no release of its own.
    world.tags = [github.Tag('v1.0', first)]
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(path)
    )
    # The rebuild exported GitHub's commit, the workflow scan read it too.
    assert signed == {WHEEL: b"__version__ = '1.0'\n"}
    epoch: str = git(path, 'log', '-1', '--format=%ct', first)
    assert runner.build_envs == [{'SOURCE_DATE_EPOCH': epoch}]
    assert world.release_shas == [first]
    assert [created[0] for created in world.created] == ['v1.0']
    # Nothing of the checkout's own refs or of origin changed.
    after: list[str] = all_refs(path).splitlines()
    assert [line for line in after if 'refs/ppt/' not in line] == (
        local_before.splitlines()
    )
    assert git(path, 'rev-parse', 'refs/tags/v1.0') == second
    assert git(path, 'rev-parse', 'refs/tags/v0.9') == third
    assert git(path, 'rev-parse', 'refs/tags/v0.1') == first
    assert all_refs(scratch_repo.origin) == origin_before
    assert remote_tags.commit(path, 'v1.0') == first
    note: str = 'local tags differ from GitHub: v0.9, v1.0'
    assert report.notes == [note]
    assert note in backfill.to_markdown(report)
    assert backfill.exit_status(report) == 0


def test_a_new_tag_whose_name_exists_only_locally_is_refused(
    scratch_repo: ScratchRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    path: pathlib.Path = scratch_repo.path
    git(path, 'tag', 'v1.1', scratch_repo.shas[1])
    before: str = git(path, 'rev-parse', 'refs/tags/v1.1')
    world: World = World()
    install_real_git(monkeypatch, world)
    world.project = project_with({'1.1': files_of('1.1')})
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(path)
    )
    plan, outcome, note = report.rows[0]
    assert (plan.version, outcome) == ('1.1', None)
    assert (
        note == 'local tag v1.1 exists and is not on GitHub, not touching it'
    )
    assert git(scratch_repo.origin, 'tag', '--list') == ''
    assert git(path, 'rev-parse', 'refs/tags/v1.1') == before
    assert remote_tags.commit(path, 'v1.1') is None
    assert not world.created
    assert report.notes == []
    assert backfill.exit_status(report) == 1


def test_a_dry_run_shows_the_refusal_and_reports_differing_tags(
    scratch_repo: ScratchRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    path: pathlib.Path = scratch_repo.path
    first, second, _ = scratch_repo.shas
    diverge(scratch_repo, 'v1.0', first, second)
    git(path, 'tag', 'v1.1', second)
    local_before: str = all_refs(path)
    world: World = World()
    install_real_git(monkeypatch, world)
    world.project = project_with({'1.1': files_of('1.1')})
    world.tags = [github.Tag('v1.0', first)]
    report: backfill.BackfillReport = backfill.run_backfill(
        'spam', options(path, dry_run=True)
    )
    rows: dict[str, str] = {
        plan.version: note for plan, _, note in report.rows
    }
    assert rows['1.1'] == (
        'local tag v1.1 exists and is not on GitHub, not touching it'
    )
    assert report.notes == ['local tags differ from GitHub: v1.0']
    after: list[str] = all_refs(path).splitlines()
    assert [line for line in after if 'refs/ppt/' not in line] == (
        local_before.splitlines()
    )
    assert not world.created


def test_the_differ_note_names_ten_tags_at_most() -> None:
    names: list[str] = [f'v1.{minor}' for minor in range(13)]
    assert remote_tags.differ_note(names) == (
        'local tags differ from GitHub: v1.0, v1.1, v1.2, v1.3, v1.4, v1.5, '
        'v1.6, v1.7, v1.8, v1.9 and 3 more'
    )
    assert remote_tags.differ_note(names[:10]).endswith('v1.9')


def test_a_tag_deleted_on_github_leaves_the_private_refs(
    scratch_repo: ScratchRepo,
) -> None:
    path: pathlib.Path = scratch_repo.path
    git(path, 'tag', 'v1.0', scratch_repo.shas[0])
    git(path, 'push', '-q', 'origin', 'v1.0')
    remote_tags.fetch(path)
    assert remote_tags.commit(path, 'v1.0') == scratch_repo.shas[0]
    git(scratch_repo.origin, 'tag', '-d', 'v1.0')
    remote_tags.fetch(path)
    assert remote_tags.commit(path, 'v1.0') is None
    assert git(path, 'rev-parse', 'refs/tags/v1.0') == scratch_repo.shas[0]
    assert git(path, 'rev-parse', 'origin/master') == scratch_repo.shas[2]
