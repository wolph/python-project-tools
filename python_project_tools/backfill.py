"""``ppt backfill``: every PyPI version on a signed GitHub release.

The command walks the PyPI versions of a package oldest first and makes sure
each one has a tag, a release and signed files. Missing tags come from
``match-tag``, missing files from ``rebuild``, signatures from ``sign``.
Versions whose commit cannot be found share one release on a root-commit
tag. The release marked latest on GitHub never changes. Call the other
modules through their attributes (``github.create_release``,
``sign.sign_release``) so tests can replace them.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import pathlib
import re
import shutil
import sys
import tarfile
import tempfile

from packaging.version import InvalidVersion, Version

from python_project_tools import (
    common,
    github,
    match_tag,
    pypi,
    rebuild,
    sign,
    signing,
    versions,
)

UNTAGGED_TAG: str = 'pypi-untagged-releases'
UNTAGGED_TITLE: str = 'PyPI versions without a matching tag'
UNKNOWN: str = 'unknown'
NO_LOCAL_TAG_NOTE: str = 'tag not in checkout, stock notes used'
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


@dataclasses.dataclass
class _Run:
    """Everything the steps of one run share."""

    package: str
    options: BackfillOptions
    project: pypi.Project
    tags: list[github.Tag]
    releases: dict[str, github.Release]
    workdir: pathlib.Path
    sign_options: sign.SignOptions
    untagged: list[Plan] = dataclasses.field(default_factory=list)


Row = tuple[Plan, sign.SignOutcome | None, str]


def _say(message: str) -> None:
    print(message)  # noqa: T201


def _fail(message: str) -> None:
    print(message, file=sys.stderr)  # noqa: T201


def preflight(repo: str) -> list[str]:
    """Reasons to refuse: enabled workflows that run on release events."""
    return [
        f'{name} is enabled and triggers on release events'
        for name in github.release_triggered_workflows(repo)
    ]


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
    try:
        done = common.run(['git', 'show', f'{tag}:{name}'], cwd=checkout)
    except UnicodeDecodeError:
        return None
    if done.returncode != 0:
        return None
    return done.stdout.splitlines()


def _local_tag_sha(checkout: pathlib.Path, tag: str) -> str | None:
    """The commit a local tag points at, or None when it does not exist."""
    done = common.run(
        ['git', 'rev-parse', '-q', '--verify', f'refs/tags/{tag}^{{commit}}'],
        cwd=checkout,
    )
    if done.returncode != 0:
        return None
    return done.stdout.strip()


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
    same kind. Returns None when the tag is not in the checkout, when no
    changelog mentions the version or when a changelog is not UTF-8.
    """
    if _local_tag_sha(checkout, tag) is None:
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


def _find_match(run: _Run, plan: Plan) -> tuple[str, pypi.PypiFile] | None:
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
    run: _Run, plan: Plan, sha: str, sdist: pypi.PypiFile
) -> str | None:
    """Create and push the tag, reusing a local tag at the same commit.

    Returns a note when a local tag of that name points somewhere else.
    """
    checkout: pathlib.Path = run.options.checkout
    name: str = versions.tag_for(plan.version)
    message: str = match_tag.TAG_MESSAGE.format(
        package=run.package,
        version=plan.version,
        filename=sdist.filename,
        sha256=sdist.sha256,
        date=dt.date.today().isoformat(),
    )
    local: str | None = _local_tag_sha(checkout, name)
    if local is not None and local != sha:
        return f'local tag {name} points at {local}, not at the matched commit'
    if local is None:
        match_tag.create_tag(checkout, name, sha, message)
    if not any(tag.name == name for tag in run.tags):
        match_tag.push_tag(checkout, name)
    plan.tag = name
    plan.tag_action = 'create'
    run.tags.append(github.Tag(name, sha))
    return None


def _plan_tag(run: _Run, plan: Plan) -> str | None:
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


def _ensure_release(run: _Run, plan: Plan) -> tuple[github.Release, str]:
    """The release of a plan, created with changelog notes when missing.

    The note is non-empty when the tag is not in the checkout, so the notes
    are the stock sentence.
    """
    assert plan.tag is not None
    existing: github.Release | None = run.releases.get(plan.tag)
    if existing is not None:
        return existing, ''
    checkout: pathlib.Path = run.options.checkout
    note: str = ''
    section: str | None = None
    if _local_tag_sha(checkout, plan.tag) is None:
        note = NO_LOCAL_TAG_NOTE
    else:
        section = changelog_section(checkout, plan.tag, plan.version)
    created: github.Release = github.create_release(
        run.options.repo,
        plan.tag,
        plan.tag,
        notes_for(plan, section),
        plan.prerelease,
    )
    run.releases[plan.tag] = created
    return created, note


def _rebuilt_dir(run: _Run, tag: str) -> pathlib.Path:
    """A sibling of the directory ``sign_release`` works in and removes."""
    return run.workdir / f'{tag}-rebuilt'


def _files_for_plan(run: _Run, plan: Plan) -> tuple[Files, dict[str, str]]:
    """The files to sign and the disclosure line of each rebuilt one."""
    if plan.files_source == 'pypi':
        return pypi.files_for(run.project, plan.version), {}
    assert plan.tag is not None
    built: list[pathlib.Path] = rebuild.rebuild(
        run.options.checkout, plan.tag, _rebuilt_dir(run, plan.tag)
    )
    note: str = REBUILT_NOTE.format(
        tag=plan.tag, date=dt.date.today().isoformat()
    )
    return built, {path.name: note for path in built}


def _release_and_sign(
    run: _Run, plan: Plan
) -> tuple[sign.SignOutcome | None, str]:
    """Rebuild or collect files, ensure the release, sign it."""
    assert plan.tag is not None
    try:
        files, rebuilt = _files_for_plan(run, plan)
        release, note = _ensure_release(run, plan)
        options: sign.SignOptions = dataclasses.replace(
            run.sign_options, rebuilt=rebuilt
        )
        outcome: sign.SignOutcome = sign.sign_release(
            release, files, run.workdir, options
        )
    except rebuild.RebuildError as error:
        return None, f'rebuild failed: {error}'
    except STEP_ERRORS as error:
        return None, f'release failed: {error}'
    finally:
        shutil.rmtree(_rebuilt_dir(run, plan.tag), ignore_errors=True)
    outcome.version = plan.version
    return outcome, note


def _dry_run_plan(
    run: _Run, plan: Plan
) -> tuple[sign.SignOutcome | None, str]:
    """Print what a plan would do and do none of it."""
    tag: str = plan.tag or versions.tag_for(plan.version)
    if plan.tag_action == 'untagged':
        return None, 'no files on PyPI and no tag'
    if plan.tag_action == 'create':
        _say(f'{plan.version}: would match tag against sdist')
        _say(f'{plan.version}: would create tag {tag}')
    if plan.release_action == 'create':
        _say(f'{plan.version}: would create release {tag}')
    if plan.files_source == 'rebuild':
        _say(f'{plan.version}: would rebuild from tag {tag}')
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
    run: _Run, plan: Plan
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


def _anchor_sha(run: _Run) -> str:
    done = common.run(
        ['git', 'rev-list', '--max-parents=0', 'HEAD'],
        cwd=run.options.checkout,
    )
    roots: list[str] = done.stdout.split()
    if done.returncode != 0 or not roots:
        raise RuntimeError(f'git rev-list: {done.stderr.strip()}')
    return roots[-1]


def _ensure_anchor_tag(run: _Run) -> str | None:
    """Make sure the untagged tag exists locally and on origin.

    Returns a note when a workflow would fire and nothing was done.
    """
    if any(tag.name == UNTAGGED_TAG for tag in run.tags):
        return None
    checkout: pathlib.Path = run.options.checkout
    sha: str = _anchor_sha(run)
    flows: list[str] = match_tag.workflow_with_tag_trigger(checkout, sha)
    if flows and not run.options.allow_workflow_trigger:
        return 'tag not pushed, workflow would fire: ' + ', '.join(flows)
    local = common.run(
        ['git', 'rev-parse', '-q', '--verify', f'refs/tags/{UNTAGGED_TAG}'],
        cwd=checkout,
    )
    if local.returncode != 0:
        match_tag.create_tag(checkout, UNTAGGED_TAG, sha, ANCHOR_MESSAGE)
    match_tag.push_tag(checkout, UNTAGGED_TAG)
    run.tags.append(github.Tag(UNTAGGED_TAG, sha))
    return None


def _untagged_release(run: _Run, plans: list[Plan]) -> github.Release:
    existing: github.Release | None = run.releases.get(UNTAGGED_TAG)
    if existing is not None:
        return existing
    listing: str = '\n'.join(f'- {plan.version}' for plan in plans)
    created: github.Release = github.create_release(
        run.options.repo,
        UNTAGGED_TAG,
        UNTAGGED_TITLE,
        f'{UNTAGGED_INTRO}\n\n{listing}',
        False,
    )
    run.releases[UNTAGGED_TAG] = created
    return created


def _handle_untagged(run: _Run) -> Row:
    """Put every untagged version on the one untagged release."""
    group: Plan = Plan(
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
    files: list[pypi.PypiFile] = [
        file
        for plan in run.untagged
        for file in pypi.files_for(run.project, plan.version)
    ]
    try:
        skipped: str | None = _ensure_anchor_tag(run)
        if skipped:
            return group, None, skipped
        release: github.Release = _untagged_release(run, run.untagged)
        outcome: sign.SignOutcome = sign.sign_release(
            release, files, run.workdir, run.sign_options
        )
    except STEP_ERRORS as error:
        return group, None, f'untagged release failed: {error}'
    outcome.version = group.version
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


def _loop(run: _Run, plans: list[Plan]) -> list[Row]:
    rows: list[Row] = []
    for plan in plans:
        outcome, note = _process_plan(run, plan)
        if plan.tag_action == 'untagged' and plan.files_source == 'pypi':
            run.untagged.append(plan)
            continue
        rows.append((plan, outcome, note))
        _say(_progress(plan, outcome, note))
    if run.untagged:
        row: Row = _handle_untagged(run)
        rows.append(row)
        _say(_progress(*row))
    return rows


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


def _fetch_tags(checkout: pathlib.Path) -> None:
    """Fetch the remote tags, never overwriting a local one."""
    done = common.run(['git', 'fetch', 'origin', '--tags'], cwd=checkout)
    if done.returncode != 0:
        raise RuntimeError(f'git fetch origin --tags: {done.stderr.strip()}')


def _sign_options(package: str, options: BackfillOptions) -> sign.SignOptions:
    return sign.SignOptions(
        repo=options.repo,
        package=package,
        gpg=options.gpg,
        sigstore=options.sigstore,
        gpg_key=options.gpg_key,
        replace_assets=options.replace_assets,
        dry_run=options.dry_run,
    )


def run_backfill(package: str, options: BackfillOptions) -> BackfillReport:
    """Backfill every version of a package and report what happened.

    The caller runs ``preflight`` first. Raises LookupError for an unknown
    package or ``only`` version, and RuntimeError or common.FetchError when
    the tags cannot be fetched or GitHub or PyPI cannot be read, all before
    anything is created.
    """
    if not options.dry_run:
        _fetch_tags(options.checkout)
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
    run: _Run = _Run(
        package=package,
        options=options,
        project=project,
        tags=list(tags),
        releases={release.tag: release for release in releases},
        workdir=workdir,
        sign_options=_sign_options(package, options),
    )
    try:
        rows: list[Row] = _loop(run, plans)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return BackfillReport(
        package=package,
        repo=options.repo,
        rows=rows,
        latest_before=before,
        latest_after=_latest_after(options.repo),
    )


def _cell(value: str) -> str:
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


def _latest_changed(report: BackfillReport) -> bool:
    return (
        report.latest_after == UNKNOWN
        or report.latest_before != report.latest_after
    )


def to_markdown(report: BackfillReport) -> str:
    """The report as a Markdown table plus the latest-release verdict."""
    lines: list[str] = [
        '| ' + ' | '.join(COLUMNS) + ' |',
        '|' + '|'.join(' --- ' for _ in COLUMNS) + '|',
    ]
    for row in report.rows:
        cells: list[str] = [_cell(cell) for cell in _row_cells(row)]
        lines.append('| ' + ' | '.join(cells) + ' |')
    before: str = report.latest_before or 'none'
    after: str = report.latest_after or 'none'
    verdict: str = 'CHANGED' if _latest_changed(report) else 'OK'
    lines.extend(['', f'Latest before: {before}, after: {after}', verdict])
    return '\n'.join(lines) + '\n'


def _build_parser() -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog='ppt backfill',
        description='Put every PyPI version on a signed GitHub release.',
        allow_abbrev=False,
    )
    parser.add_argument('package')
    parser.add_argument('--repo', help='OWNER/NAME, default from PyPI')
    parser.add_argument(
        '--checkout', type=pathlib.Path, default=pathlib.Path()
    )
    parser.add_argument('--only', metavar='VERSION')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--replace-assets', action='store_true')
    only = parser.add_mutually_exclusive_group()
    only.add_argument('--gpg-only', action='store_true')
    only.add_argument('--sigstore-only', action='store_true')
    parser.add_argument('--gpg-key', default=signing.DEFAULT_GPG_KEY)
    parser.add_argument('--allow-workflow-trigger', action='store_true')
    return parser


def _resolve_repo(args: argparse.Namespace) -> str | None:
    if args.repo:
        return str(args.repo)
    return pypi.project(args.package).repo


def _exit_status(report: BackfillReport) -> int:
    """1 when a version failed or was skipped or latest moved, else 0."""
    for _, outcome, _ in report.rows:
        if outcome is None or outcome.status == 'failed':
            return 1
    return 1 if _latest_changed(report) else 0


def main(argv: list[str]) -> int:
    """Run ``ppt backfill`` and return its exit status."""
    args: argparse.Namespace = _build_parser().parse_args(argv)
    if not args.checkout.is_dir():
        _fail(f'--checkout {args.checkout} is not a directory')
        return 2
    try:
        repo: str | None = _resolve_repo(args)
        if not repo:
            _fail(f'no --repo and PyPI has none for {args.package}')
            return 2
        reasons: list[str] = preflight(repo)
        if reasons:
            for reason in reasons:
                _fail(reason)
            return 2
        options: BackfillOptions = BackfillOptions(
            repo=repo,
            checkout=args.checkout,
            only=args.only,
            dry_run=args.dry_run,
            replace_assets=args.replace_assets,
            gpg=not args.sigstore_only,
            sigstore=not args.gpg_only,
            gpg_key=args.gpg_key,
            allow_workflow_trigger=args.allow_workflow_trigger,
        )
        report: BackfillReport = run_backfill(args.package, options)
    except (LookupError, RuntimeError, common.FetchError) as error:
        _fail(str(error))
        return 2
    _say(to_markdown(report))
    return _exit_status(report)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
