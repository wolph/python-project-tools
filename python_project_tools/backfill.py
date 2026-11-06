"""``ppt backfill``: every PyPI version on a signed GitHub release.

The command walks the PyPI versions of a package oldest first and makes sure
each one has a tag, a release and signed files. Missing tags come from
``match-tag``, missing files from ``rebuild``, signatures from ``sign``.
A rebuilt release that an interrupted run left with a signed SHA256SUMS is
completed from its own files, checked against that list, because a rebuild
need not give the same bytes.
Versions whose commit cannot be found share one release on a root-commit
tag. The release marked latest on GitHub never changes. GitHub marks the
first stable release of an empty repository as latest whatever
``--latest=false`` says, so a repository with no latest release releases its
highest stable version first, and a run stops when the latest release is
anything else after the first release it created. GitHub can report no
Latest for a moment after that release, so a missing Latest is read again a
few times before the run decides. The target is the
highest stable version that gets a release of its own: a version whose
commit cannot be matched joins the untagged anchor and the next one down is
tried, and only a failure after a release was created stops the run. The
anchor is the target when no version gets its own release. The target is
marked latest at the end when GitHub does not report it already. The
Sigstore bundles of every release are made after the loop, in one sigstore
invocation for up to SIGSTORE_CHUNK files, so one browser login covers the
run. A run goes through ``start_run``, ``execute_run``, ``sign_pending``
and ``finish_run``, so ``backfill_batch`` can sign the bundles of several
packages in one invocation. ``run_backfill`` is the run of one package.
The tags on GitHub are the truth: they are fetched into the private
``remote_tags.REMOTE_TAGS`` namespace, every tag is read from there, and
the checkout's own ``refs/tags`` are never written or moved. A new tag
whose name already exists locally is refused, and local tags that differ
from GitHub's are named in the report.
Call the other modules through their attributes (``github.create_release``,
``sign.sign_release``) so tests can replace them.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import pathlib
import re
import shutil
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable

from packaging.version import InvalidVersion, Version

from python_project_tools import (
    common,
    github,
    match_tag,
    pypi,
    rebuild,
    remote_tags,
    sign,
    signing,
    versions,
)

UNTAGGED_TAG: str = 'pypi-untagged-releases'
UNTAGGED_TITLE: str = 'PyPI versions without a matching tag'
UNKNOWN: str = 'unknown'
NO_LOCAL_TAG_NOTE: str = 'tag not fetched from GitHub, stock notes used'
ON_GITHUB_NOTE: str = 'tag {name} already exists on GitHub, not touching it'
STOCK_NOTES: str = (
    'Release {version}. No changelog section was found at tag {tag}.'
)
YANKED_NOTE: str = 'This version was yanked on PyPI.'
ANCHOR_MESSAGE: str = (
    'Anchor for PyPI versions whose commit could not be matched.'
)
UNTAGGED_INTRO: str = (
    'PyPI versions for which no matching commit could be found. Their files '
    'are the exact PyPI uploads.'
)
REBUILT_NOTE: str = '# rebuilt from tag {tag} on {date}, not the PyPI bytes'
REBUILD_SKIPPED_NOTE: str = 'rebuild skipped, the release is already signed'
RESUMED_NOTE: str = (
    'rebuild skipped, files taken from the release and checked against its '
    'signed SHA256SUMS'
)
SUMS_SIGNATURE: str = f'{signing.SHA256SUMS_NAME}.asc'
RELEASE_WORKFLOW_NOTE: str = 'release would fire {names} at the tagged commit'
LATEST_MARKED: str = (
    'GitHub marked {tag} as Latest despite --latest=false, stopping'
)
LATEST_NOT_TARGET: str = (
    'after creating {created} GitHub reports Latest {actual}, '
    'expected {expected}, stopping'
)
TARGET_FAILED: str = (
    'target {version} failed after its release was created, stopping'
)
# What became of a Latest candidate.
RELEASED: str = 'released'
FELL_THROUGH: str = 'fell through'
FAILED_AFTER_CREATING: str = 'failed after creating'
# Files per sigstore invocation. Each invocation is one browser login, so
# the cap only keeps the argv of one invocation a sane length.
SIGSTORE_CHUNK: int = 2000
# GitHub can answer releases/latest with 404 for a moment after the first
# release of a repository, then with that release. The guard reads Latest
# this many times at most while it is missing, sleeping in between.
LATEST_READS: int = 5
LATEST_READ_INTERVAL: float = 2.0
# Called through the module attribute so tests can replace it.
sleep: Callable[[float], None] = time.sleep
SUMS_DIR: str = 'sums'
CHANGELOG_NAMES: tuple[str, ...] = (
    'CHANGELOG.rst',
    'CHANGELOG.md',
    'CHANGES.rst',
    'CHANGES.md',
    'HISTORY.rst',
    'HISTORY.md',
)
RST_HEADING: re.Pattern[str] = re.compile(r'^[0-9][\w.+-]*:$')
MD_HEADING: re.Pattern[str] = re.compile(r'^(#+)\s')
COLUMNS: tuple[str, ...] = (
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
)
# Failures a single version can raise without stopping the run.
STEP_ERRORS: tuple[type[Exception], ...] = (
    RuntimeError,
    common.FetchError,
    pypi.IntegrityError,
    OSError,  # a missing git or uv binary, a full disk
    tarfile.TarError,  # a bad git archive
    UnicodeDecodeError,  # a changelog or git output that is not UTF-8
)

Files = list[pypi.PypiFile] | list[pathlib.Path]


@dataclasses.dataclass
class Plan:
    """What one version needs."""

    version: str
    tag: str | None
    tag_action: str  # 'exists', 'create', 'untagged'
    release_action: str  # 'exists', 'create'
    files_source: str  # 'pypi', 'rebuild', 'none'
    prerelease: bool
    yanked: bool


@dataclasses.dataclass
class BackfillOptions:
    """Settings of one run."""

    repo: str
    checkout: pathlib.Path
    only: str | None = None
    dry_run: bool = False
    replace_assets: bool = False
    gpg: bool = True
    sigstore: bool = True
    gpg_key: str = signing.DEFAULT_GPG_KEY
    allow_workflow_trigger: bool = False


@dataclasses.dataclass
class BackfillReport:
    """The result of one run."""

    package: str
    repo: str
    rows: list[tuple[Plan, sign.SignOutcome | None, str]]
    latest_before: str | None
    latest_after: str | None
    # The release that must be Latest afterwards: latest_before when there
    # was one, else the highest stable version with files, if any.
    latest_target: str | None = None
    # Why the run stopped early, empty when it ran to the end.
    stopped: str = ''
    # Lines about the package as a whole, such as differing local tags.
    notes: list[str] = dataclasses.field(default_factory=list)


class RunStoppedError(RuntimeError):
    """The run must not go on creating releases.

    Raised past the per-version error handling so the run stops.
    """


class LatestMarkedError(RunStoppedError):
    """GitHub's Latest is not the release this run wanted it to be."""


class TargetFailedError(RunStoppedError):
    """The Latest target failed after its release may have been created."""


class ResumeError(RuntimeError):
    """The signed files on a release cannot be trusted to resume from.

    The version fails: rebuilding instead would give bytes that differ
    from the ones already signed.
    """


Row = tuple[Plan, sign.SignOutcome | None, str]


@dataclasses.dataclass
class _Pending:
    """A signed release still waiting for its Sigstore bundles."""

    release: github.Release
    outcome: sign.SignOutcome
    rebuilt_names: list[str]
    options: sign.SignOptions


@dataclasses.dataclass
class PackageRun:
    """Everything the steps of one run share.

    ``workdir`` holds the staged copies the deferred Sigstore bundles are
    made for, so it lives until ``close_run``.
    """

    package: str
    options: BackfillOptions
    project: pypi.Project
    tags: list[github.Tag]
    releases: dict[str, github.Release]
    workdir: pathlib.Path
    sign_options: sign.SignOptions
    latest_before: str | None = None
    plans: list[Plan] = dataclasses.field(default_factory=list)
    # Why the run stopped early, empty while it has not.
    stopped: str = ''
    untagged: list[Plan] = dataclasses.field(default_factory=list)
    rows: list[Row] = dataclasses.field(default_factory=list)
    # The plan that must become Latest. With no Latest release it is the
    # candidate being tried, then the one that got its own release, or the
    # untagged anchor when no candidate did.
    target: Plan | None = None
    created_any: bool = False
    # Tags a release was requested for, whether or not the call returned.
    attempted: set[str] = dataclasses.field(default_factory=set)
    pending: list[_Pending] = dataclasses.field(default_factory=list)
    disabled_workflows: set[str] | None = None
    notes: list[str] = dataclasses.field(default_factory=list)


def _say(message: str) -> None:
    print(message)  # noqa: T201


def _fail(message: str) -> None:
    print(message, file=sys.stderr)  # noqa: T201


def preflight(repo: str) -> list[str]:
    """Reasons to refuse: an archived repository, release workflows.

    A repository whose metadata cannot be read is refused as well, since
    nothing then says it is not archived.
    """
    reasons: list[str] = []
    raw: object | None = common.gh_json(['api', f'repos/{repo}'])
    if not isinstance(raw, dict):
        reasons.append(f'could not read repos/{repo}')
    elif raw.get('archived'):
        reasons.append(f'{repo} is archived, run: gh repo unarchive {repo}')
    reasons.extend(
        f'{name} is enabled and triggers on release events'
        for name in github.release_triggered_workflows(repo)
    )
    return reasons


def _is_prerelease(version: str) -> bool:
    try:
        return versions.is_prerelease(version)
    except InvalidVersion:
        return False


def _order(version: str) -> tuple[int, Version, str]:
    """Sort key: PEP 440 order first, unparsable versions last."""
    try:
        return (0, versions.sort_key(version), '')
    except InvalidVersion:
        return (1, Version('0'), version)


def _pypi_plan(
    version: str,
    files: list[pypi.PypiFile],
    tags: list[github.Tag],
    released: set[str],
) -> Plan:
    tag: github.Tag | None = versions.match_tag(version, tags)
    tag_action: str = 'exists'
    if tag is None:
        tag_action = 'create' if files else 'untagged'
    source: str = 'pypi' if files else ('rebuild' if tag else 'none')
    has_release: bool = tag is not None and tag.name in released
    return Plan(
        version=version,
        tag=None if tag is None else tag.name,
        tag_action=tag_action,
        release_action='exists' if has_release else 'create',
        files_source=source,
        prerelease=_is_prerelease(version),
        yanked=bool(files) and all(f.yanked for f in files),
    )


def _tag_only_plans(
    project: pypi.Project, tags: list[github.Tag], released: set[str]
) -> list[Plan]:
    """Plans for version-shaped tags that match no PyPI version."""
    known: set[str] = {versions.normalise(v) for v in project.releases}
    plans: list[Plan] = []
    for tag in tags:
        version: str = versions.normalise(tag.name)
        if not versions.is_version_tag(tag.name) or version in known:
            continue
        known.add(version)
        plans.append(
            Plan(
                version=version,
                tag=tag.name,
                tag_action='exists',
                release_action='exists' if tag.name in released else 'create',
                files_source='rebuild',
                prerelease=_is_prerelease(version),
                yanked=False,
            )
        )
    return plans


def plan_versions(
    project: pypi.Project,
    tags: list[github.Tag],
    releases: list[github.Release],
) -> list[Plan]:
    """One plan per PyPI version and per version tag PyPI does not know."""
    released: set[str] = {release.tag for release in releases}
    plans: list[Plan] = [
        _pypi_plan(version, pypi.files_for(project, version), tags, released)
        for version in project.releases
    ]
    plans.extend(_tag_only_plans(project, tags, released))
    return sorted(plans, key=lambda plan: _order(plan.version))


def _show(checkout: pathlib.Path, tag: str, name: str) -> list[str] | None:
    """The lines of a file at GitHub's tag, None when it cannot be read."""
    source: str = f'{remote_tags.ref(tag)}:{name}'
    try:
        done = common.run(['git', 'show', source], cwd=checkout)
    except UnicodeDecodeError:
        return None
    if done.returncode != 0:
        return None
    return done.stdout.splitlines()


def _rst_block(lines: list[str], version: str) -> list[str] | None:
    heading: str = f'{version}:'
    for start, line in enumerate(lines):
        if line.rstrip() != heading:
            continue
        end: int = len(lines)
        for index in range(start + 1, len(lines)):
            if RST_HEADING.match(lines[index].rstrip()):
                end = index
                break
        return lines[start:end]
    return None


def _md_block(lines: list[str], version: str) -> list[str] | None:
    wanted: re.Pattern[str] = re.compile(rf'^(#+)\s*v?{re.escape(version)}\b')
    for start, line in enumerate(lines):
        found: re.Match[str] | None = wanted.match(line)
        if found is None:
            continue
        level: int = len(found.group(1))
        end: int = len(lines)
        for index in range(start + 1, len(lines)):
            other: re.Match[str] | None = MD_HEADING.match(lines[index])
            if other is not None and len(other.group(1)) <= level:
                end = index
                break
        return lines[start:end]
    return None


def changelog_section(
    checkout: pathlib.Path, tag: str, version: str
) -> str | None:
    """The changelog block of a version as it stood at a tag.

    Matches ``VERSION:`` headings (reStructuredText changelogs) and
    ``## VERSION`` headings (Markdown) and runs to the next heading of the
    same kind. The tag is GitHub's, from ``remote_tags.REMOTE_TAGS``.
    Returns None when that tag was not fetched, when no changelog mentions
    the version or when a changelog is not UTF-8.
    """
    if remote_tags.commit(checkout, tag) is None:
        return None
    for name in CHANGELOG_NAMES:
        lines: list[str] | None = _show(checkout, tag, name)
        if lines is None:
            continue
        block: list[str] | None = _rst_block(lines, version) or _md_block(
            lines, version
        )
        if block is not None:
            return '\n'.join(block).rstrip()
    return None


def notes_for(plan: Plan, section: str | None) -> str:
    """The release notes: the changelog block or the stock sentence."""
    body: str = section or STOCK_NOTES.format(
        version=plan.version, tag=plan.tag
    )
    if plan.yanked:
        return f'{YANKED_NOTE}\n\n{body}'
    return body


def _sdist_of(files: list[pypi.PypiFile]) -> pypi.PypiFile | None:
    for file in files:
        if file.packagetype == 'sdist':
            return file
    return None


def _find_match(
    run: PackageRun, plan: Plan
) -> tuple[str, pypi.PypiFile] | None:
    """The commit whose files equal the sdist, with the sdist, or None."""
    sdist: pypi.PypiFile | None = _sdist_of(
        pypi.files_for(run.project, plan.version)
    )
    if sdist is None:
        return None
    checkout: pathlib.Path = run.options.checkout
    with tempfile.TemporaryDirectory(dir=run.workdir) as work:
        archive: pathlib.Path = pypi.download(sdist, pathlib.Path(work))
        cands: list[match_tag.Candidate] = [
            match_tag.compare(checkout, sha, archive)
            for sha in match_tag.candidates(checkout, plan.version)
        ]
    best: match_tag.Candidate | None = match_tag.choose(
        cands, sdist.upload_time
    )
    return None if best is None else (best.sha, sdist)


def _make_tag(
    run: PackageRun, plan: Plan, sha: str, sdist: pypi.PypiFile
) -> str | None:
    """Create and push a new tag, refusing a name that is already taken.

    Returns a note, and creates nothing, when GitHub already has a tag of
    that name or when a local tag of that name exists, whatever it points
    at. An existing tag is never moved, replaced or pushed.
    """
    checkout: pathlib.Path = run.options.checkout
    name: str = versions.tag_for(plan.version)
    refused: str | None = _taken_tag_note(run, name)
    if refused is not None:
        return refused
    message: str = match_tag.TAG_MESSAGE.format(
        package=run.package,
        version=plan.version,
        filename=sdist.filename,
        sha256=sdist.sha256,
        date=dt.date.today().isoformat(),
    )
    match_tag.create_tag(checkout, name, sha, message, key=run.options.gpg_key)
    match_tag.push_tag(checkout, name)
    plan.tag = name
    plan.tag_action = 'create'
    run.tags.append(github.Tag(name, sha))
    return None


def _taken_tag_note(run: PackageRun, name: str) -> str | None:
    """Why a new tag ``name`` must not be created, or None when it may.

    GitHub's tags are checked in the API listing and in the fetched
    private refs, the checkout's own tags by name alone.
    """
    checkout: pathlib.Path = run.options.checkout
    on_github: bool = any(tag.name == name for tag in run.tags)
    if on_github or remote_tags.commit(checkout, name) is not None:
        return ON_GITHUB_NOTE.format(name=name)
    if remote_tags.local_object(checkout, name) is not None:
        return match_tag.LOCAL_TAG_EXISTS.format(name=name)
    return None


def _plan_tag(run: PackageRun, plan: Plan) -> str | None:
    """Create the tag of a version, returning a note when it is skipped.

    A version whose commit cannot be matched becomes 'untagged' and returns
    no note.
    """
    try:
        found: tuple[str, pypi.PypiFile] | None = _find_match(run, plan)
        if found is None:
            plan.tag_action = 'untagged'
            return None
        sha, sdist = found
        flows: list[str] = match_tag.workflow_with_tag_trigger(
            run.options.checkout, sha
        )
        if flows and not run.options.allow_workflow_trigger:
            return 'tag not pushed, workflow would fire: ' + ', '.join(flows)
        return _make_tag(run, plan, sha, sdist)
    except STEP_ERRORS as error:
        return f'tag creation failed: {error}'
    return None


def _read_latest_after(run: PackageRun, tag: str) -> str | None:
    """The tag of Latest right after creating ``tag``, or None.

    A Latest that cannot be read raises LatestMarkedError: the run cannot
    show that it is right.
    """
    try:
        latest: github.Release | None = github.latest_release(run.options.repo)
    except (RuntimeError, common.FetchError) as error:
        raise LatestMarkedError(
            f'could not read Latest after creating {tag}: {error}, stopping'
        ) from error
    return None if latest is None else latest.tag


def _settled_latest(run: PackageRun, tag: str) -> str | None:
    """Latest after creating ``tag``, read again while GitHub reports none.

    GitHub can lag a second behind the release it just created, so a
    missing Latest is read up to LATEST_READS times, LATEST_READ_INTERVAL
    seconds apart. A Latest that is there is returned at once, right or
    wrong, so a wrong one is never waited on.
    """
    actual: str | None = _read_latest_after(run, tag)
    for _ in range(LATEST_READS - 1):
        if actual is not None:
            break
        sleep(LATEST_READ_INTERVAL)
        actual = _read_latest_after(run, tag)
    return actual


def _check_latest_is_target(run: PackageRun, tag: str) -> None:
    """Stop unless Latest is the target after the first created release.

    The target is released first, so Latest must be its tag then. A run
    without a target expects no Latest at all. A Latest that cannot be read
    counts as wrong: the run cannot show that it is right. The decision is
    made on the last of the reads ``_settled_latest`` makes.
    """
    expected: str | None = None if run.target is None else run.target.tag
    actual: str | None = _settled_latest(run, tag)
    if actual == expected:
        return
    if expected is None:
        raise LatestMarkedError(LATEST_MARKED.format(tag=actual))
    raise LatestMarkedError(
        LATEST_NOT_TARGET.format(
            created=tag, actual=actual or 'none', expected=expected
        )
    )


def _pin_latest_target(run: PackageRun) -> None:
    """Mark the target Latest explicitly once the guard accepted it.

    The automatic first Latest is recomputed by GitHub from release dates
    and versions, so a later release made with --latest=false can still
    take it over, for example when old tags meet a new anchor tag. An
    explicit mark pins it. A run without a target has nothing to pin.
    """
    if run.target is None or run.target.tag is None:
        return
    tag: str = run.target.tag
    try:
        github.mark_latest(run.options.repo, tag)
    except (RuntimeError, common.FetchError) as error:
        raise LatestMarkedError(
            f'could not pin {tag} as Latest after creating it: {error}, '
            'stopping'
        ) from error


def _create_release(
    run: PackageRun, tag: str, title: str, notes: str, prerelease: bool
) -> github.Release:
    """Create a release, checking Latest after the first one of a run.

    Only a repository without a Latest release before the run is checked:
    that is where GitHub promotes the first stable release despite
    --latest=false, so it must be the target.
    """
    run.attempted.add(tag)
    created: github.Release = github.create_release(
        run.options.repo, tag, title, notes, prerelease
    )
    run.releases[tag] = created
    first: bool = not run.created_any
    run.created_any = True
    if first and run.latest_before is None:
        _check_latest_is_target(run, tag)
        _pin_latest_target(run)
    return created


def _tag_commit(run: PackageRun, tag: str) -> str:
    """The commit of GitHub's tag, from the fetched refs or the API listing.

    The checkout's own tag of that name is never read.
    """
    fetched: str | None = remote_tags.commit(run.options.checkout, tag)
    if fetched is not None:
        return fetched
    for known in run.tags:
        if known.name == tag and known.sha:
            return known.sha
    raise RuntimeError(f'the commit of {tag} is unknown')


def _disabled_workflows(run: PackageRun) -> set[str]:
    if run.disabled_workflows is None:
        run.disabled_workflows = github.disabled_workflow_paths(
            run.options.repo
        )
    return run.disabled_workflows


def _release_workflow_note(run: PackageRun, tag: str) -> str | None:
    """A refusal when a new release at ``tag`` would run a workflow.

    GitHub runs release workflows from the tagged commit, so the files at
    that commit count. Only a workflow GitHub lists as disabled is safe: an
    active, deleted or unlisted one is refused.
    """
    paths: list[str] = match_tag.workflow_with_release_trigger(
        run.options.checkout, _tag_commit(run, tag)
    )
    if not paths:
        return None
    disabled: set[str] = _disabled_workflows(run)
    firing: list[str] = [
        pathlib.PurePosixPath(path).name
        for path in paths
        if path not in disabled
    ]
    if not firing:
        return None
    return RELEASE_WORKFLOW_NOTE.format(names=', '.join(firing))


def _ensure_release(run: PackageRun, plan: Plan) -> tuple[github.Release, str]:
    """The release of a plan, created with changelog notes when missing.

    The note is non-empty when GitHub's tag was not fetched, so the notes
    are the stock sentence.
    """
    assert plan.tag is not None
    existing: github.Release | None = run.releases.get(plan.tag)
    if existing is not None:
        return existing, ''
    checkout: pathlib.Path = run.options.checkout
    note: str = ''
    section: str | None = None
    if remote_tags.commit(checkout, plan.tag) is None:
        note = NO_LOCAL_TAG_NOTE
    else:
        section = changelog_section(checkout, plan.tag, plan.version)
    created: github.Release = _create_release(
        run, plan.tag, plan.tag, notes_for(plan, section), plan.prerelease
    )
    return created, note


def _rebuilt_dir(run: PackageRun, tag: str) -> pathlib.Path:
    """A sibling of the directory ``sign_release`` works in and removes."""
    return run.workdir / f'{tag}-rebuilt'


def _resumable(release: github.Release | None) -> bool:
    """Whether a release carries a SHA256SUMS list and its signature."""
    if release is None:
        return False
    names: set[str] = {asset.name for asset in release.assets}
    return {sign.SUMS_NAME, SUMS_SIGNATURE} <= names


def _listed_name_ok(name: str) -> bool:
    """A plain dist file name, not a path and not a signature or sums file."""
    if name in ('', '.', '..') or '/' in name or '\\' in name:
        return False
    if name in (sign.SUMS_NAME, SUMS_SIGNATURE):
        return False
    return not name.endswith(('.asc', signing.BUNDLE_SUFFIX))


def _fetch_listed(
    assets: dict[str, github.Asset],
    name: str,
    digest: str,
    into: pathlib.Path,
) -> pathlib.Path:
    """Download one dist SHA256SUMS lists and check it against the list."""
    if not _listed_name_ok(name):
        raise ResumeError(f'SHA256SUMS lists a bad file name {name!r}')
    asset: github.Asset | None = assets.get(name)
    if asset is None:
        raise ResumeError(f'{name} is in SHA256SUMS but not on the release')
    path: pathlib.Path = github.download_asset(asset, into)
    actual: str = common.sha256_of(path)
    if actual != digest.lower():
        raise ResumeError(
            f'{name} on the release has sha256 {actual}, SHA256SUMS says '
            f'{digest}'
        )
    return path


def _verified_sums(
    run: PackageRun, tag: str, assets: dict[str, github.Asset]
) -> pathlib.Path:
    """Download SHA256SUMS and raise unless the configured key signed it."""
    into: pathlib.Path = run.workdir / SUMS_DIR / tag
    sums: pathlib.Path = github.download_asset(assets[sign.SUMS_NAME], into)
    signature: pathlib.Path = github.download_asset(
        assets[SUMS_SIGNATURE], into
    )
    key: str = run.options.gpg_key
    if not signing.gpg_verify_key(sums, signature, key):
        raise ResumeError(f'SHA256SUMS signature not valid for {key}')
    return sums


def _resume_files(
    run: PackageRun, tag: str
) -> tuple[list[pathlib.Path], dict[str, str]] | None:
    """The dists of an earlier, interrupted run, taken from the release.

    None when the release has no SHA256SUMS with a signature, so the files
    must be rebuilt. Otherwise the list must carry a good signature by
    the configured key, and every dist it names is downloaded from the
    release and checked against it. Their rebuilt comments come back as
    well, so the files keep their marking. Any failure raises ResumeError:
    a rebuild would not give the bytes that are already signed.
    """
    release: github.Release | None = run.releases.get(tag)
    if release is None or not _resumable(release):
        return None
    assets: dict[str, github.Asset] = {a.name: a for a in release.assets}
    sums: pathlib.Path = _verified_sums(run, tag, assets)
    listed: dict[str, str] = signing.parse_sha256sums(sums)
    if not listed:
        raise ResumeError('SHA256SUMS lists no files')
    comments: dict[str, str] = signing.sha256sums_comments(sums)
    unmarked: list[str] = [name for name in listed if name not in comments]
    if unmarked:
        raise ResumeError(
            'SHA256SUMS does not mark as rebuilt: ' + ', '.join(unmarked)
        )
    into: pathlib.Path = _rebuilt_dir(run, tag)
    dists: list[pathlib.Path] = [
        _fetch_listed(assets, name, digest, into)
        for name, digest in listed.items()
    ]
    return dists, comments


def _files_for_plan(
    run: PackageRun, plan: Plan
) -> tuple[Files, dict[str, str], str]:
    """The files to sign, the disclosure line of each rebuilt one, a note.

    A rebuild plan whose release already has a signed SHA256SUMS resumes
    from the files on that release instead of rebuilding them.
    """
    if plan.files_source == 'pypi':
        return pypi.files_for(run.project, plan.version), {}, ''
    assert plan.tag is not None
    resumed: tuple[list[pathlib.Path], dict[str, str]] | None = _resume_files(
        run, plan.tag
    )
    if resumed is not None:
        return resumed[0], resumed[1], RESUMED_NOTE
    built: list[pathlib.Path] = rebuild.rebuild(
        run.options.checkout, plan.tag, _rebuilt_dir(run, plan.tag)
    )
    note: str = REBUILT_NOTE.format(
        tag=plan.tag, date=dt.date.today().isoformat()
    )
    return built, {path.name: note for path in built}, ''


def _signed_names(entries: list[str], options: sign.SignOptions) -> set[str]:
    """Every asset a release signed by this tool has for these dists."""
    names: set[str] = {sign.SUMS_NAME, *entries}
    if options.gpg:
        names.add(f'{sign.SUMS_NAME}.asc')
        names.update(f'{entry}.asc' for entry in entries)
    if options.sigstore:
        names.update(f'{entry}{signing.BUNDLE_SUFFIX}' for entry in entries)
    return names


def _already_signed(run: PackageRun, plan: Plan) -> sign.SignOutcome | None:
    """An outcome when an existing release needs no rebuild at all.

    That is when its body carries the footer, its SHA256SUMS lists entries
    and the release has each of them with the signatures this run would
    make. Rebuilding then only produces bytes that differ from the files
    already signed. A release without the footer was interrupted before
    its end, so it goes on to be completed.
    """
    assert plan.tag is not None
    release: github.Release = run.releases[plan.tag]
    if sign.FOOTER_MARKER not in release.body:
        return None
    assets: dict[str, github.Asset] = {a.name: a for a in release.assets}
    sums: github.Asset | None = assets.get(sign.SUMS_NAME)
    if sums is None:
        return None
    path: pathlib.Path = github.download_asset(
        sums, run.workdir / SUMS_DIR / plan.tag
    )
    entries: list[str] = sorted(signing.parse_sha256sums(path))
    wanted: set[str] = _signed_names(entries, run.sign_options)
    if not entries or not wanted <= set(assets):
        return None
    return sign.SignOutcome(
        tag=plan.tag,
        version=plan.version,
        uploaded=[],
        kept=sorted(wanted),
        conflicts=[],
        status='already signed',
        reason='',
    )


def _precheck(
    run: PackageRun, plan: Plan
) -> tuple[sign.SignOutcome | None, str] | None:
    """A finished row for a plan that needs no rebuild or must not go on.

    A skipped row (no outcome) when creating the release would fire a
    workflow, a done row when a rebuild plan's release is already signed,
    and None when the plan goes ahead.
    """
    assert plan.tag is not None
    if plan.tag not in run.releases:
        refused: str | None = _release_workflow_note(run, plan.tag)
        return None if refused is None else (None, refused)
    if plan.files_source == 'rebuild':
        signed: sign.SignOutcome | None = _already_signed(run, plan)
        if signed is not None:
            return signed, REBUILD_SKIPPED_NOTE
    return None


def _release_and_sign(
    run: PackageRun, plan: Plan
) -> tuple[sign.SignOutcome | None, str]:
    """Rebuild or collect files, ensure the release, sign it."""
    assert plan.tag is not None
    try:
        early: tuple[sign.SignOutcome | None, str] | None = _precheck(
            run, plan
        )
        if early is not None:
            return early
        files, rebuilt, files_note = _files_for_plan(run, plan)
        release, note = _ensure_release(run, plan)
        options: sign.SignOptions = dataclasses.replace(
            run.sign_options, rebuilt=rebuilt
        )
        outcome: sign.SignOutcome = sign.sign_release(
            release, files, run.workdir, options
        )
    except rebuild.RebuildError as error:
        return None, f'rebuild failed: {error}'
    except RunStoppedError:
        raise
    except ResumeError as error:
        return _failed(plan, str(error)), ''
    except STEP_ERRORS as error:
        return None, f'release failed: {error}'
    finally:
        shutil.rmtree(_rebuilt_dir(run, plan.tag), ignore_errors=True)
    outcome.version = plan.version
    _remember_pending(run, release, outcome, files, options)
    return outcome, note or files_note


def _failed(plan: Plan, reason: str) -> sign.SignOutcome:
    """The outcome of a version that failed before anything was uploaded."""
    assert plan.tag is not None
    return sign.SignOutcome(
        tag=plan.tag,
        version=plan.version,
        uploaded=[],
        kept=[],
        conflicts=[],
        status='failed',
        reason=reason,
    )


def _remember_pending(
    run: PackageRun,
    release: github.Release,
    outcome: sign.SignOutcome,
    files: Files,
    options: sign.SignOptions,
) -> None:
    """Queue a release whose Sigstore bundles wait for the end of the run."""
    if not outcome.pending_bundles:
        return
    rebuilt_names: list[str] = [
        f.name for f in files if isinstance(f, pathlib.Path)
    ]
    run.pending.append(_Pending(release, outcome, rebuilt_names, options))


def _dry_run_plan(
    run: PackageRun, plan: Plan
) -> tuple[sign.SignOutcome | None, str]:
    """Print what a plan would do and do none of it."""
    tag: str = plan.tag or versions.tag_for(plan.version)
    if plan.tag_action == 'untagged':
        return None, 'no files on PyPI and no tag'
    if plan.tag_action == 'create':
        taken: str | None = _taken_tag_note(run, tag)
        if taken is not None:
            return None, taken
        _say(f'{plan.version}: would match tag against sdist')
        _say(f'{plan.version}: would create tag {tag}')
    if plan.release_action == 'create':
        _say(f'{plan.version}: would create release {tag}')
    if plan.files_source == 'rebuild':
        _say(f'{plan.version}: would {_dry_source(run, tag)}')
        _say(f'{plan.version}: would sign the rebuilt files')
        return _dry_outcome(tag, plan.version), 'dry run'
    files: list[pypi.PypiFile] = pypi.files_for(run.project, plan.version)
    _say(f'{plan.version}: would download {len(files)} files')
    _say(f'{plan.version}: would sign')
    release: github.Release = run.releases.get(tag) or github.Release(
        tag=tag,
        id=0,
        body='',
        prerelease=plan.prerelease,
        draft=False,
        assets=[],
    )
    outcome: sign.SignOutcome = sign.sign_release(
        release, files, run.workdir, run.sign_options
    )
    outcome.version = plan.version
    return outcome, 'dry run'


def _dry_source(run: PackageRun, tag: str) -> str:
    """Where a dry run says the files of a rebuild plan would come from."""
    if _resumable(run.releases.get(tag)):
        return f'check the files on release {tag} against its SHA256SUMS'
    return f'rebuild from tag {tag}'


def _dry_outcome(tag: str, version: str) -> sign.SignOutcome:
    return sign.SignOutcome(
        tag=tag,
        version=version,
        uploaded=[],
        kept=[],
        conflicts=[],
        status='dry run',
        reason='',
    )


def _process_plan(
    run: PackageRun, plan: Plan
) -> tuple[sign.SignOutcome | None, str]:
    """Do everything one version needs, returning its outcome and a note.

    A version that joins the untagged group returns no outcome and no note,
    its plan then reads 'untagged' with files from PyPI.
    """
    if run.options.dry_run:
        return _dry_run_plan(run, plan)
    if plan.tag_action == 'create':
        skipped: str | None = _plan_tag(run, plan)
        if skipped:
            return None, skipped
    if plan.tag_action == 'untagged':
        if plan.files_source == 'none':
            return None, 'no files on PyPI and no tag'
        return None, ''
    return _release_and_sign(run, plan)


def _anchor_sha(run: PackageRun) -> str:
    """A root commit of the history GitHub has, never of a local branch."""
    done = common.run(
        [
            *('git', 'rev-list', '--max-parents=0'),
            *remote_tags.GITHUB_GLOBS,
        ],
        cwd=run.options.checkout,
    )
    roots: list[str] = done.stdout.split()
    if done.returncode != 0 or not roots:
        reason: str = done.stderr.strip() or 'no root commit on GitHub'
        raise RuntimeError(f'git rev-list: {reason}')
    return roots[-1]


def _ensure_anchor_tag(run: PackageRun) -> str | None:
    """Make sure the untagged tag exists on GitHub, creating it if not.

    Returns a note, and does nothing, when a workflow would fire or when a
    tag of that name exists locally or was fetched from GitHub.
    """
    if any(tag.name == UNTAGGED_TAG for tag in run.tags):
        return None
    checkout: pathlib.Path = run.options.checkout
    refused: str | None = _taken_tag_note(run, UNTAGGED_TAG)
    if refused is not None:
        return refused
    sha: str = _anchor_sha(run)
    flows: list[str] = match_tag.workflow_with_tag_trigger(checkout, sha)
    if flows and not run.options.allow_workflow_trigger:
        return 'tag not pushed, workflow would fire: ' + ', '.join(flows)
    match_tag.create_tag(
        checkout,
        UNTAGGED_TAG,
        sha,
        ANCHOR_MESSAGE,
        key=run.options.gpg_key,
    )
    match_tag.push_tag(checkout, UNTAGGED_TAG)
    run.tags.append(github.Tag(UNTAGGED_TAG, sha))
    return None


def _untagged_release(run: PackageRun, plans: list[Plan]) -> github.Release:
    existing: github.Release | None = run.releases.get(UNTAGGED_TAG)
    if existing is not None:
        return existing
    listing: str = '\n'.join(f'- {plan.version}' for plan in plans)
    return _create_release(
        run,
        UNTAGGED_TAG,
        UNTAGGED_TITLE,
        f'{UNTAGGED_INTRO}\n\n{listing}',
        False,
    )


def _untagged_group(run: PackageRun) -> Plan:
    """The one row that stands for every untagged version."""
    return Plan(
        version=', '.join(plan.version for plan in run.untagged),
        tag=UNTAGGED_TAG,
        tag_action=(
            'exists'
            if any(tag.name == UNTAGGED_TAG for tag in run.tags)
            else 'create'
        ),
        release_action='exists' if UNTAGGED_TAG in run.releases else 'create',
        files_source='pypi',
        prerelease=False,
        yanked=False,
    )


def _handle_untagged(run: PackageRun, group: Plan) -> Row:
    """Put every untagged version on the one untagged release."""
    files: list[pypi.PypiFile] = [
        file
        for plan in run.untagged
        for file in pypi.files_for(run.project, plan.version)
    ]
    options: sign.SignOptions = dataclasses.replace(
        run.sign_options, multi_version=True
    )
    try:
        skipped: str | None = _ensure_anchor_tag(run)
        if not skipped and UNTAGGED_TAG not in run.releases:
            skipped = _release_workflow_note(run, UNTAGGED_TAG)
        if skipped:
            return group, None, skipped
        release: github.Release = _untagged_release(run, run.untagged)
        outcome: sign.SignOutcome = sign.sign_release(
            release, files, run.workdir, options
        )
    except RunStoppedError:
        raise
    except STEP_ERRORS as error:
        return group, None, f'untagged release failed: {error}'
    outcome.version = group.version
    _remember_pending(run, release, outcome, files, options)
    return group, outcome, ''


def _selected(plans: list[Plan], only: str | None) -> list[Plan]:
    if only is None:
        return plans
    wanted: str = versions.normalise(only)
    chosen: list[Plan] = [
        plan
        for plan in plans
        if plan.version == only or versions.normalise(plan.version) == wanted
    ]
    if not chosen:
        raise LookupError(f'no version {only} on PyPI or as a tag')
    return chosen


def _add_row(run: PackageRun, row: Row) -> None:
    run.rows.append(row)
    _say(_progress(*row))


def _stopping(run: PackageRun, plan: Plan, error: RunStoppedError) -> None:
    """Record the plan that stopped the run, before the error moves on."""
    _add_row(run, (plan, None, str(error)))


def _verdict(
    run: PackageRun, plan: Plan, outcome: sign.SignOutcome | None
) -> str:
    """What became of a candidate that was processed.

    A release this run requested that is missing or whose signing failed
    may have left a release that is not Latest, so only a candidate that
    created nothing falls through to the next one.
    """
    tag: str | None = plan.tag
    if tag is None:
        return FELL_THROUGH
    if tag in run.attempted:
        failed: bool = outcome is None or outcome.status == 'failed'
        if tag not in run.releases or failed:
            return FAILED_AFTER_CREATING
        return RELEASED
    return RELEASED if tag in run.releases else FELL_THROUGH


def _stop_failed_target(
    run: PackageRun,
    plan: Plan,
    outcome: sign.SignOutcome | None,
    note: str,
    rest: list[Plan],
) -> None:
    """Record the failed target and skip every other plan, then stop.

    Anything created now could become Latest in the target's place. Plans
    already sent to the untagged group get a skipped row as well.
    """
    reason: str = TARGET_FAILED.format(version=plan.version)
    _add_row(run, (plan, outcome, note))
    for other in [*run.untagged, *rest]:
        _add_row(run, (other, None, reason))
    raise TargetFailedError(reason)


def _step(run: PackageRun, plan: Plan) -> tuple[sign.SignOutcome | None, str]:
    """Process one plan, recording the plan that stopped the run."""
    try:
        return _process_plan(run, plan)
    except RunStoppedError as error:
        _stopping(run, plan, error)
        raise


def _record(
    run: PackageRun, plan: Plan, outcome: sign.SignOutcome | None, note: str
) -> None:
    """Queue an untagged version for the anchor, else add its row."""
    if plan.tag_action == 'untagged' and plan.files_source == 'pypi':
        run.untagged.append(plan)
        return
    _add_row(run, (plan, outcome, note))


def _release_target(
    run: PackageRun, plans: list[Plan], candidates: list[Plan]
) -> list[Plan]:
    """Try the candidates from the highest down until one has its release.

    Returns the plans left for the oldest first pass. A candidate that
    created nothing (no matching commit, no files, a workflow refusal)
    falls through. A candidate that failed after creating something stops
    the run. Without a winner the target is cleared.
    """
    tried: list[Plan] = []
    for plan in candidates:
        run.target = plan
        outcome, note = _step(run, plan)
        tried.append(plan)
        verdict: str = _verdict(run, plan, outcome)
        rest: list[Plan] = [p for p in plans if all(p is not t for t in tried)]
        if verdict == FAILED_AFTER_CREATING:
            _stop_failed_target(run, plan, outcome, note, rest)
        _record(run, plan, outcome, note)
        if verdict == RELEASED:
            return rest
    run.target = None
    return [p for p in plans if all(p is not t for t in tried)]


def _finish_untagged(run: PackageRun) -> None:
    """Release the untagged anchor last, as the target when nothing else is."""
    if not run.untagged:
        return
    group: Plan = _untagged_group(run)
    if run.target is None and run.latest_before is None:
        run.target = group
    try:
        row: Row = _handle_untagged(run, group)
    except RunStoppedError as error:
        _stopping(run, group, error)
        raise
    _add_row(run, row)


def _loop(run: PackageRun, plans: list[Plan], candidates: list[Plan]) -> None:
    """Process every plan into ``run.rows``, then the untagged group.

    A RunStoppedError stops the loop with the rows so far kept. A dry run
    only orders the plans, it cannot know which candidate would win.
    """
    if run.options.dry_run:
        rest: list[Plan] = _first_candidate_first(plans, candidates)
    else:
        rest = _release_target(run, plans, candidates)
    for plan in rest:
        outcome, note = _step(run, plan)
        _record(run, plan, outcome, note)
    _finish_untagged(run)


def _chunks(paths: list[pathlib.Path], size: int) -> list[list[pathlib.Path]]:
    """Groups of at most ``size`` paths, no file name twice in a group.

    sigstore stages one group in one directory, so names must not clash.
    """
    chunks: list[list[pathlib.Path]] = []
    names: list[set[str]] = []
    for path in paths:
        for chunk, seen in zip(chunks, names, strict=True):
            if len(chunk) < size and path.name not in seen:
                chunk.append(path)
                seen.add(path.name)
                break
        else:
            chunks.append([path])
            names.append({path.name})
    return chunks


def _sign_bundles(paths: list[pathlib.Path]) -> dict[pathlib.Path, str]:
    """Sign every path, one sigstore invocation per chunk.

    Returns the error of each path whose chunk failed.
    """
    failed: dict[pathlib.Path, str] = {}
    for chunk in _chunks(paths, SIGSTORE_CHUNK):
        error: str = _sign_chunk(chunk)
        if error:
            failed.update(dict.fromkeys(chunk, error))
    return failed


def _sign_chunk(chunk: list[pathlib.Path]) -> str:
    """One sigstore invocation, returning its error or an empty string."""
    try:
        signing.sigstore_sign(chunk)
    except (signing.SigningError, RuntimeError, OSError) as error:
        return str(error) or type(error).__name__
    return ''


def sign_pending(runs: list[PackageRun]) -> None:
    """Make the deferred Sigstore bundles of every run and upload them.

    All files of all runs go to one sigstore invocation per SIGSTORE_CHUNK
    files, so one browser login covers them. A stopped run is included:
    its releases exist and only their bundles are missing.
    """
    pending: list[_Pending] = [entry for run in runs for entry in run.pending]
    if not pending:
        return
    failed: dict[pathlib.Path, str] = _sign_bundles(
        [path for entry in pending for path in entry.outcome.pending_bundles]
    )
    for entry in pending:
        errors: list[str] = [
            failed[path]
            for path in entry.outcome.pending_bundles
            if path in failed
        ]
        if errors:
            entry.outcome.status = 'failed'
            entry.outcome.reason = f'sigstore failed: {errors[0]}'
        else:
            sign.finish_pending(
                entry.release,
                entry.outcome,
                entry.rebuilt_names,
                entry.options,
            )
        _say(f'{entry.release.tag}: {entry.outcome.status}')
    for run in runs:
        run.pending.clear()


def _progress(plan: Plan, outcome: sign.SignOutcome | None, note: str) -> str:
    status: str = 'skipped' if outcome is None else outcome.status
    line: str = f'{plan.version}: {status}'
    return f'{line}: {note}' if note else line


def _latest_tag(repo: str) -> str | None:
    latest: github.Release | None = github.latest_release(repo)
    return None if latest is None else latest.tag


def _latest_after(repo: str) -> str | None:
    """The latest release after the run, UNKNOWN when GitHub cannot say."""
    try:
        return _latest_tag(repo)
    except (RuntimeError, common.FetchError):
        return UNKNOWN


def _fetch_tags(checkout: pathlib.Path) -> list[str]:
    """Fetch GitHub's tags into the private refs, return the report notes.

    The checkout's own tags are left alone. Local tags that differ from
    GitHub's tag of the same name get one note line, they are not changed.
    """
    remote_tags.fetch(checkout)
    names: list[str] = remote_tags.differing(checkout)
    return [remote_tags.differ_note(names)] if names else []


def _sign_options(package: str, options: BackfillOptions) -> sign.SignOptions:
    return sign.SignOptions(
        repo=options.repo,
        package=package,
        gpg=options.gpg,
        sigstore=options.sigstore,
        gpg_key=options.gpg_key,
        replace_assets=options.replace_assets,
        dry_run=options.dry_run,
        # One sigstore login for the whole run instead of one per release.
        sigstore_later=options.sigstore and not options.dry_run,
    )


def _is_stable(plan: Plan) -> bool:
    """A parsable version that is neither a pre-release nor yanked."""
    try:
        versions.sort_key(plan.version)
    except InvalidVersion:
        return False
    return not plan.prerelease and not plan.yanked


def _candidates(run: PackageRun, plans: list[Plan]) -> list[Plan]:
    """The Latest candidates, highest first, when there is no Latest yet.

    GitHub marks the first stable release of an empty repository Latest, so
    that release must be the target. A candidate is a stable plan with
    files (PyPI or a rebuild). Whether it gets a release of its own is only
    known once it ran, see ``_release_target``. A repository with a Latest
    release keeps its order and its Latest.
    """
    if run.latest_before is not None:
        return []
    candidates: list[Plan] = [
        plan
        for plan in plans
        if _is_stable(plan) and plan.files_source != 'none'
    ]
    return sorted(
        candidates,
        key=lambda plan: versions.sort_key(plan.version),
        reverse=True,
    )


def _first_candidate_first(
    plans: list[Plan], candidates: list[Plan]
) -> list[Plan]:
    """The order when the top candidate gets its release."""
    if not candidates:
        return plans
    return [candidates[0], *(p for p in plans if p is not candidates[0])]


def _announce(
    run: PackageRun, plans: list[Plan], candidates: list[Plan]
) -> None:
    """Print the target candidates and the order of a dry run."""
    if candidates:
        _say(
            'target candidates: '
            + ', '.join(plan.version for plan in candidates)
        )
    elif run.latest_before is not None:
        _say(f'target: {run.latest_before} (the existing Latest)')
    else:
        _say('target: none')
    _say(
        'order: '
        + ', '.join(
            plan.version for plan in _first_candidate_first(plans, candidates)
        )
    )


def _mark_target(run: PackageRun) -> str | None:
    """Mark the target Latest unless GitHub already reports it.

    Returns the tag that must be Latest now, or None without a target.
    """
    if run.target is None or run.target.tag is None:
        return None
    tag: str = run.target.tag
    try:
        if _latest_tag(run.options.repo) != tag:
            github.mark_latest(run.options.repo, tag)
    except (RuntimeError, common.FetchError) as error:
        # The report then shows Latest differing from the target.
        _fail(f'could not mark {tag} as Latest: {error}')
    return tag


def execute_run(run: PackageRun) -> None:
    """Run the plans, leaving the Sigstore bundles to ``sign_pending``.

    A RunStoppedError ends the loop with the rows so far kept and its
    message in ``run.stopped``.
    """
    candidates: list[Plan] = _candidates(run, run.plans)
    if run.options.dry_run:
        _announce(run, run.plans, candidates)
    try:
        _loop(run, run.plans, candidates)
    except RunStoppedError as error:
        run.stopped = str(error)


def _final_target(run: PackageRun) -> str | None:
    """The tag that must be Latest, marking it when the run may.

    A stopped run, a dry run and a repository that had a Latest release
    mark nothing.
    """
    if run.stopped or run.latest_before is not None or run.options.dry_run:
        return run.latest_before
    return _mark_target(run)


def start_run(package: str, options: BackfillOptions) -> PackageRun:
    """Read everything a run needs and plan it, creating nothing.

    The caller runs ``preflight`` first. Raises LookupError for an unknown
    package or ``only`` version, and RuntimeError or common.FetchError when
    the tags cannot be fetched or GitHub or PyPI cannot be read. A dry run
    fetches the tags too: it only writes the private refs, and it then
    fails where the real run would. The run owns a work directory from
    here on, ``close_run`` removes it.
    """
    notes: list[str] = _fetch_tags(options.checkout)
    for note in notes:
        _say(note)
    project: pypi.Project = pypi.project(package)
    tags: list[github.Tag] = github.tags(options.repo)
    releases: list[github.Release] = github.releases(options.repo)
    before: str | None = _latest_tag(options.repo)
    plans: list[Plan] = _selected(
        plan_versions(project, tags, releases), options.only
    )
    workdir: pathlib.Path = pathlib.Path(
        tempfile.mkdtemp(prefix='ppt-backfill-')
    )
    return PackageRun(
        package=package,
        options=options,
        project=project,
        tags=list(tags),
        releases={release.tag: release for release in releases},
        workdir=workdir,
        sign_options=_sign_options(package, options),
        latest_before=before,
        plans=plans,
        notes=notes,
    )


def finish_run(run: PackageRun) -> BackfillReport:
    """Mark the Latest target when the run may, then report the run.

    Runs after ``sign_pending``, so the target already has its bundles.
    """
    target: str | None = _final_target(run)
    return BackfillReport(
        package=run.package,
        repo=run.options.repo,
        rows=run.rows,
        latest_before=run.latest_before,
        latest_after=_latest_after(run.options.repo),
        latest_target=target,
        stopped=run.stopped,
        notes=list(run.notes),
    )


def close_run(run: PackageRun) -> None:
    """Remove the work directory of a run."""
    shutil.rmtree(run.workdir, ignore_errors=True)


def run_backfill(package: str, options: BackfillOptions) -> BackfillReport:
    """Backfill every version of one package and report what happened.

    Raises what ``start_run`` raises, before anything is created. A run
    that GitHub's Latest marking stops returns the rows so far with
    ``stopped`` set, its created releases still get their bundles.
    """
    run: PackageRun = start_run(package, options)
    try:
        execute_run(run)
        sign_pending([run])
        return finish_run(run)
    finally:
        close_run(run)


def markdown_cell(value: str) -> str:
    """A value made safe for one cell of a Markdown table row."""
    return value.replace('|', '\\|').replace('\n', ' ')


def _row_cells(row: Row) -> list[str]:
    plan, outcome, note = row
    counts: list[str] = ['', '', '']
    status: str = 'skipped'
    if outcome is not None:
        status = outcome.status
        counts = [
            str(len(outcome.uploaded)),
            str(len(outcome.kept)),
            str(len(outcome.conflicts)),
        ]
        if outcome.status == 'failed' and outcome.reason:
            note = f'{outcome.reason}, {note}' if note else outcome.reason
    return [
        plan.version,
        plan.tag or '',
        plan.tag_action,
        plan.release_action,
        plan.files_source,
        status,
        *counts,
        note,
    ]


def latest_changed(report: BackfillReport) -> bool:
    """Whether Latest is anything but the target, or cannot be read."""
    return (
        report.latest_after == UNKNOWN
        or report.latest_after != report.latest_target
    )


def to_markdown(report: BackfillReport) -> str:
    """The report as a Markdown table plus the latest-release verdict."""
    lines: list[str] = [
        '| ' + ' | '.join(COLUMNS) + ' |',
        '|' + '|'.join(' --- ' for _ in COLUMNS) + '|',
    ]
    for row in report.rows:
        cells: list[str] = [markdown_cell(cell) for cell in _row_cells(row)]
        lines.append('| ' + ' | '.join(cells) + ' |')
    lines.append('')
    lines.extend(report.notes)
    if report.stopped:
        lines.append(f'Stopped: {report.stopped}')
    before: str = report.latest_before or 'none'
    after: str = report.latest_after or 'none'
    target: str = report.latest_target or 'none'
    verdict: str = 'CHANGED' if latest_changed(report) else 'OK'
    lines.extend(
        [f'Latest before: {before}, after: {after}, target: {target}', verdict]
    )
    return '\n'.join(lines) + '\n'


def row_failed(outcome: sign.SignOutcome | None) -> bool:
    """Whether a row is skipped, failed or still waits for its bundles."""
    return outcome is None or outcome.status in ('failed', sign.PENDING)


def exit_status(report: BackfillReport) -> int:
    """2 when the run stopped, 1 when a version failed or was skipped,
    still waits for bundles or Latest is off target, else 0.
    """
    if report.stopped:
        return 2
    if any(row_failed(outcome) for _, outcome, _ in report.rows):
        return 1
    return 1 if latest_changed(report) else 0
