"""``ppt sign``: GPG and Sigstore signatures on a GitHub release.

Files are taken from PyPI (sha256 verified) or from local rebuilds, signed,
and attached to the release next to a ``SHA256SUMS`` list. Call the other
modules through their attributes (``pypi.download``, ``github.upload_assets``)
so tests can replace them.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import pathlib
import shutil
import sys
import tempfile

from python_project_tools import common, github, pypi, signing, versions

FOOTER_MARKER: str = '<!-- ppt-signed -->'
VERIFY_URL: str = (
    'https://github.com/WoLpH/python-project-tools/blob/master/VERIFY.md'
)
SUMS_NAME: str = signing.SHA256SUMS_NAME
EXISTING_DIR: str = 'existing'
HANDLED_ERRORS: tuple[type[Exception], ...] = (
    pypi.IntegrityError,
    signing.SigningError,
    RuntimeError,
    common.FetchError,
)


@dataclasses.dataclass
class SignOutcome:
    """What happened to one release."""

    tag: str
    version: str
    uploaded: list[str]
    kept: list[str]  # same name, same bytes, already there
    conflicts: list[str]  # same name, different bytes
    status: str  # 'signed', 'already signed', 'failed'
    reason: str


@dataclasses.dataclass
class SignOptions:
    """Settings shared by every release of one run."""

    repo: str
    package: str
    gpg: bool = True
    sigstore: bool = True
    gpg_key: str = signing.DEFAULT_GPG_KEY
    replace_assets: bool = False
    dry_run: bool = False
    rebuilt: dict[str, str] = dataclasses.field(default_factory=dict)
    signed_on: dt.date = dataclasses.field(default_factory=dt.date.today)


def footer(options: SignOptions, rebuilt_names: list[str]) -> str:
    """The note appended to a release body once it is signed."""
    rebuilt: str = ', '.join(rebuilt_names) or 'none'
    return (
        f'{FOOTER_MARKER}\n'
        f'Signed on {options.signed_on.isoformat()} with `ppt sign`, after '
        'the release. The files above are\n'
        'the exact PyPI uploads for this version, verified by sha256 '
        'against PyPI\n'
        'before signing. Each file has a GPG signature (`.asc`, key '
        f'{options.gpg_key}) and a\n'
        'Sigstore bundle (`.sigstore.json`). `SHA256SUMS.asc` signs the '
        'checksum\n'
        f'list. Rebuilt files, if any: {rebuilt}. How to verify: '
        f'{VERIFY_URL}\n'
    )


def _fail(message: str) -> None:
    print(message, file=sys.stderr)  # noqa: T201


def _say(message: str) -> None:
    print(message)  # noqa: T201


def _signature_names(dist_names: list[str], options: SignOptions) -> list[str]:
    """Names of every signature and sums file this run would produce."""
    names: list[str] = []
    for name in dist_names:
        if options.gpg:
            names.append(f'{name}.asc')
        if options.sigstore:
            names.append(f'{name}.sigstore.json')
    names.append(SUMS_NAME)
    if options.gpg:
        names.append(f'{SUMS_NAME}.asc')
    return names


def _local_dists(
    files: list[pypi.PypiFile] | list[pathlib.Path],
    workdir: pathlib.Path,
) -> list[pathlib.Path]:
    """Download PyPI files or place local ones, all in ``workdir``."""
    paths: list[pathlib.Path] = []
    for item in files:
        if isinstance(item, pathlib.Path):
            target: pathlib.Path = workdir / item.name
            if item.resolve() != target.resolve():
                shutil.copy2(item, target)
            paths.append(target)
        else:
            paths.append(pypi.download(item, workdir))
    return paths


def _compare_dists(
    release: github.Release,
    dists: list[pathlib.Path],
    workdir: pathlib.Path,
    options: SignOptions,
    outcome: SignOutcome,
) -> tuple[list[pathlib.Path], list[pathlib.Path]]:
    """Split dist files into (new, to clobber) against existing assets."""
    existing: dict[str, github.Asset] = {a.name: a for a in release.assets}
    new: list[pathlib.Path] = []
    clobber: list[pathlib.Path] = []
    for path in dists:
        asset: github.Asset | None = existing.get(path.name)
        if asset is None:
            new.append(path)
            continue
        fetched: pathlib.Path = github.download_asset(
            asset, workdir / EXISTING_DIR
        )
        if common.sha256_of(fetched) == common.sha256_of(path):
            outcome.kept.append(path.name)
            continue
        outcome.conflicts.append(path.name)
        if options.replace_assets:
            clobber.append(path)
    return new, clobber


def _needed(name: str, present: set[str], options: SignOptions) -> bool:
    """Whether an output file is missing from the release or replaceable."""
    return options.replace_assets or name not in present


def _make_signatures(
    dists: list[pathlib.Path],
    workdir: pathlib.Path,
    options: SignOptions,
    present: set[str],
) -> list[pathlib.Path]:
    """Produce only the signature and sums files the release still lacks."""
    produced: list[pathlib.Path] = []
    if options.gpg:
        produced.extend(
            signing.gpg_sign(p, options.gpg_key)
            for p in dists
            if _needed(f'{p.name}.asc', present, options)
        )
    if options.sigstore:
        todo: list[pathlib.Path] = [
            p
            for p in dists
            if _needed(f'{p.name}.sigstore.json', present, options)
        ]
        if todo:
            produced.extend(signing.sigstore_sign(todo))
    # The list and its signature are a pair: if either is missing both are
    # regenerated, so the uploaded signature matches the uploaded list.
    pair_missing: bool = _needed(SUMS_NAME, present, options) or (
        options.gpg and _needed(f'{SUMS_NAME}.asc', present, options)
    )
    if pair_missing:
        sums: pathlib.Path = signing.write_sha256sums(
            workdir, dists, options.rebuilt
        )
        produced.append(sums)
        if options.gpg:
            produced.append(signing.gpg_sign(sums, options.gpg_key))
    return produced


def _split_signatures(
    produced: list[pathlib.Path],
    present: set[str],
    dist_names: list[str],
    options: SignOptions,
    outcome: SignOutcome,
) -> tuple[list[pathlib.Path], list[pathlib.Path]]:
    """Produced files to upload plainly and with clobber, plus kept names."""
    new: list[pathlib.Path] = [p for p in produced if p.name not in present]
    pair: set[str] = {SUMS_NAME, f'{SUMS_NAME}.asc'}
    clobber: list[pathlib.Path] = [
        p
        for p in produced
        if p.name in present and (options.replace_assets or p.name in pair)
    ]
    replaced: set[str] = {p.name for p in clobber}
    outcome.kept.extend(
        name
        for name in _signature_names(dist_names, options)
        if name in present and name not in replaced
    )
    return new, clobber


def _upload(
    release: github.Release,
    new: list[pathlib.Path],
    clobber: list[pathlib.Path],
    options: SignOptions,
    outcome: SignOutcome,
) -> None:
    if new:
        github.upload_assets(options.repo, release.tag, new)
        outcome.uploaded.extend(p.name for p in new)
    if clobber:
        github.upload_assets(options.repo, release.tag, clobber, clobber=True)
        outcome.uploaded.extend(p.name for p in clobber)


def _append_footer(
    release: github.Release,
    rebuilt_names: list[str],
    options: SignOptions,
) -> None:
    if FOOTER_MARKER in release.body:
        return
    text: str = footer(options, rebuilt_names)
    body: str = (
        f'{release.body.rstrip()}\n\n{text}' if release.body.strip() else text
    )
    github.edit_notes(options.repo, release.tag, body)


def _finish(outcome: SignOutcome) -> SignOutcome:
    if outcome.uploaded:
        outcome.status = 'signed'
    else:
        outcome.status = 'already signed'
    return outcome


def _dry_run(
    release: github.Release,
    files: list[pypi.PypiFile] | list[pathlib.Path],
    options: SignOptions,
    outcome: SignOutcome,
) -> SignOutcome:
    """Print every action of a run and perform none."""
    present: set[str] = {a.name for a in release.assets}
    dist_names: list[str] = [
        item.name if isinstance(item, pathlib.Path) else item.filename
        for item in files
    ]
    for item in files:
        if not isinstance(item, pathlib.Path):
            _say(f'would download {item.filename}')
    for name in dist_names:
        if name in present:
            _say(f'would download existing {name} to compare')
            outcome.kept.append(name)
    wanted: list[str] = [
        n for n in dist_names if n not in present
    ] + _signature_names(dist_names, options)
    for name in wanted:
        if name not in present or options.replace_assets:
            _say(f'would upload {name}')
            outcome.uploaded.append(name)
        else:
            outcome.kept.append(name)
    if FOOTER_MARKER not in release.body:
        _say('would append footer')
    return _finish(outcome)


def sign_release(
    release: github.Release,
    files: list[pypi.PypiFile] | list[pathlib.Path],
    workdir: pathlib.Path,
    options: SignOptions,
) -> SignOutcome:
    """Sign one release, never raising for the handled failure kinds.

    Works in ``workdir / release.tag`` and removes it afterwards.
    """
    outcome: SignOutcome = SignOutcome(
        tag=release.tag,
        version=versions.normalise(release.tag),
        uploaded=[],
        kept=[],
        conflicts=[],
        status='',
        reason='',
    )
    if not files:
        outcome.status = 'failed'
        outcome.reason = 'no files for this version'
        return outcome
    if options.dry_run:
        return _dry_run(release, files, options, outcome)
    rebuilt_names: list[str] = [
        f.name for f in files if isinstance(f, pathlib.Path)
    ]
    workdir = workdir / release.tag
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        dists: list[pathlib.Path] = _local_dists(files, workdir)
        new, clobber = _compare_dists(
            release, dists, workdir, options, outcome
        )
        if outcome.conflicts and not options.replace_assets:
            outcome.status = 'failed'
            outcome.reason = 'conflicting assets left alone: ' + ', '.join(
                outcome.conflicts
            )
            return outcome
        present: set[str] = {a.name for a in release.assets}
        produced: list[pathlib.Path] = _make_signatures(
            dists, workdir, options, present
        )
        sig_new, sig_clobber = _split_signatures(
            produced, present, [d.name for d in dists], options, outcome
        )
        _upload(
            release, new + sig_new, clobber + sig_clobber, options, outcome
        )
        _append_footer(release, rebuilt_names, options)
    except HANDLED_ERRORS as error:
        outcome.status = 'failed'
        outcome.reason = str(error)
        return outcome
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return _finish(outcome)


def releases_missing_signatures(
    repo: str, project: pypi.Project
) -> list[github.Release]:
    """Releases of a PyPI version that lack a ``.asc`` for any of its files."""
    all_releases: list[github.Release] = github.releases(repo)
    tags: list[github.Tag] = [github.Tag(r.tag, '') for r in all_releases]
    by_tag: dict[str, github.Release] = {r.tag: r for r in all_releases}
    missing: list[github.Release] = []
    seen: set[str] = set()
    for version, files in project.releases.items():
        tag: github.Tag | None = versions.match_tag(version, tags)
        if tag is None or not files or tag.name in seen:
            continue
        seen.add(tag.name)
        release: github.Release = by_tag[tag.name]
        names: set[str] = {a.name for a in release.assets}
        if any(f'{f.filename}.asc' not in names for f in files):
            missing.append(release)
    return missing


def _version_for(tag: str, project: pypi.Project) -> str | None:
    wanted: str = versions.normalise(tag)
    for version in project.releases:
        if versions.normalise(version) == wanted:
            return version
    return None


def _build_parser() -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog='ppt sign',
        description='Sign the files of a GitHub release.',
        allow_abbrev=False,
    )
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument('tag', nargs='?', help='release tag to sign')
    which.add_argument(
        '--all', action='store_true', help='every release lacking signatures'
    )
    parser.add_argument('--repo', help='OWNER/NAME, default from PyPI')
    parser.add_argument('--package', help='default from pyproject.toml')
    only = parser.add_mutually_exclusive_group()
    only.add_argument('--gpg-only', action='store_true')
    only.add_argument('--sigstore-only', action='store_true')
    parser.add_argument('--gpg-key', default=signing.DEFAULT_GPG_KEY)
    parser.add_argument('--replace-assets', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    return parser


def _resolve_targets(
    args: argparse.Namespace, options: SignOptions, project: pypi.Project
) -> list[github.Release]:
    """The releases to sign: one tag, or every unsigned one."""
    if args.all:
        return releases_missing_signatures(options.repo, project)
    found: list[github.Release] = [
        r for r in github.releases(options.repo) if r.tag == args.tag
    ]
    if not found:
        raise LookupError(f'no release {args.tag} in {options.repo}')
    return found


def _report(outcome: SignOutcome) -> None:
    line: str = (
        f'{outcome.tag}: {outcome.status} (uploaded {len(outcome.uploaded)}, '
        f'kept {len(outcome.kept)}, conflicts {len(outcome.conflicts)})'
    )
    if outcome.status == 'failed':
        line += f': {outcome.reason}'
    _say(line)


def _sign_one(
    release: github.Release,
    project: pypi.Project,
    workdir: pathlib.Path,
    options: SignOptions,
) -> SignOutcome:
    version: str | None = _version_for(release.tag, project)
    if version is None:
        return SignOutcome(
            tag=release.tag,
            version='',
            uploaded=[],
            kept=[],
            conflicts=[],
            status='failed',
            reason=f'{release.tag} matches no PyPI version',
        )
    outcome: SignOutcome = sign_release(
        release, pypi.files_for(project, version), workdir, options
    )
    outcome.version = version
    return outcome


def _sign_all(
    targets: list[github.Release],
    project: pypi.Project,
    options: SignOptions,
) -> list[SignOutcome]:
    outcomes: list[SignOutcome] = []
    workdir: pathlib.Path = pathlib.Path(tempfile.mkdtemp(prefix='ppt-sign-'))
    try:
        for release in targets:
            outcome: SignOutcome = _sign_one(
                release, project, workdir, options
            )
            _report(outcome)
            outcomes.append(outcome)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return outcomes


def main(argv: list[str]) -> int:
    """Run ``ppt sign`` and return its exit status."""
    args: argparse.Namespace = _build_parser().parse_args(argv)
    package: str = args.package or common.read_package_name()
    if not package:
        _fail('no --package and no pyproject.toml here')
        return 2
    try:
        project: pypi.Project = pypi.project(package)
        repo: str | None = args.repo or project.repo
        if not repo:
            _fail(f'no --repo and PyPI has none for {package}')
            return 2
        options: SignOptions = SignOptions(
            repo=repo,
            package=package,
            gpg=not args.sigstore_only,
            sigstore=not args.gpg_only,
            gpg_key=args.gpg_key,
            replace_assets=args.replace_assets,
            dry_run=args.dry_run,
        )
        targets: list[github.Release] = _resolve_targets(
            args, options, project
        )
    except (LookupError, RuntimeError, common.FetchError) as error:
        _fail(str(error))
        return 2
    outcomes: list[SignOutcome] = _sign_all(targets, project, options)
    ok: bool = all(o.status in ('signed', 'already signed') for o in outcomes)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
