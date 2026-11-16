"""``ppt sign``: GPG and Sigstore signatures on a GitHub release.

Files are taken from PyPI (sha256 verified) or from local rebuilds, signed,
and attached to the release next to a ``SHA256SUMS`` list. By default the
Sigstore bundles come from the repository's sign.yml workflow (see
``ci_bundles``), the browser signer only runs with ``--sigstore-browser``.
Call the other modules through their attributes (``pypi.download``,
``github.upload_assets``) so tests can replace them.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import pathlib
import shutil
import sys
import tempfile
import typing

from python_project_tools import (
    ci_bundles,
    ci_sign,
    common,
    github,
    pypi,
    signing,
    versions,
)
from python_project_tools.ci_bundles import NoWorkflowError

# 'ci': bundles from sign.yml, 'browser': signed here through a browser
# login, 'off': no Sigstore bundles at all.
SigstoreMode = typing.Literal['ci', 'browser', 'off']

FOOTER_MARKER: str = '<!-- ppt-signed -->'
VERIFY_URL: str = (
    'https://github.com/WoLpH/python-project-tools/blob/master/VERIFY.md'
)
SUMS_NAME: str = signing.SHA256SUMS_NAME
EXISTING_DIR: str = 'existing'
EXIT_NO_WORKFLOW: int = 3
BUNDLES_DIR: str = 'bundles'
PENDING: str = 'bundles pending'
FILES_PYPI: str = (
    'The files above are the exact PyPI uploads for this version, verified '
    'by sha256 against PyPI before signing.'
)
FILES_WITH_REBUILT: str = (
    'The files above are the PyPI uploads for this version where PyPI has '
    'them, verified by sha256 against PyPI before signing, except the '
    'rebuilt files listed below, which were built from the tag and are '
    'marked in SHA256SUMS.'
)
FILES_MULTI_VERSION: str = (
    'The files above are the exact PyPI uploads of the versions listed in '
    'these notes, each verified by sha256 against PyPI before signing.'
)
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
    # 'signed', 'already signed', 'failed', or 'bundles pending' until
    # finish_pending has uploaded the deferred Sigstore bundles.
    status: str
    reason: str
    # Copies of the dists that still need a Sigstore bundle, outside the
    # per-release directory, when SignOptions.sigstore_later is set.
    pending_bundles: list[pathlib.Path] = dataclasses.field(
        default_factory=list
    )


@dataclasses.dataclass
class SignOptions:
    """Settings shared by every release of one run."""

    repo: str
    package: str
    gpg: bool = True
    sigstore: SigstoreMode = 'ci'
    gpg_key: str = signing.DEFAULT_GPG_KEY
    replace_assets: bool = False
    dry_run: bool = False
    rebuilt: dict[str, str] = dataclasses.field(default_factory=dict)
    signed_on: dt.date = dataclasses.field(default_factory=dt.date.today)
    # Leave the Sigstore bundles to the caller, who signs the files of many
    # releases in one invocation and then calls finish_pending.
    sigstore_later: bool = False
    # The release holds the files of several versions (the untagged anchor).
    multi_version: bool = False
    # The sign.yml of the repository, checked once per run by main. Mode
    # 'ci' looks it up per release when it is left out.
    ci_workflow: ci_bundles.Workflow | None = None


def _files_sentence(options: SignOptions, rebuilt_names: list[str]) -> str:
    """What the footer says about where the files came from."""
    if options.multi_version:
        return FILES_MULTI_VERSION
    if rebuilt_names:
        return FILES_WITH_REBUILT
    return FILES_PYPI


def _bundles_sentence(options: SignOptions, by_workflow: bool) -> str:
    """What the footer says about the signatures of each file."""
    bundle: str = 'a Sigstore bundle (`.sigstore.json`)'
    if by_workflow:
        # Points the reader at the CI section of VERIFY.md.
        bundle += ' made by the sign.yml workflow of this repository'
    return (
        f'Each file has a GPG signature (`.asc`, key {options.gpg_key}) and '
        f'{bundle}.'
    )


def footer(
    options: SignOptions, rebuilt_names: list[str], by_workflow: bool = False
) -> str:
    """The note appended to a release body once it is signed.

    One sentence per line, so Markdown renders one paragraph. The marker
    line comes first, and rebuilt files are listed after the paragraph.
    ``by_workflow`` says the repository's sign.yml made every bundle.
    """
    lines: list[str] = [
        FOOTER_MARKER,
        f'Signed on {options.signed_on.isoformat()} with `ppt sign`, after '
        'the release.',
        _files_sentence(options, rebuilt_names),
        _bundles_sentence(options, by_workflow),
        '`SHA256SUMS.asc` signs the checksum list.',
    ]
    if not rebuilt_names and not options.multi_version:
        lines.append('Rebuilt files, if any: none.')
    lines.append(f'How to verify: {VERIFY_URL}')
    text: str = '\n'.join(lines) + '\n'
    if rebuilt_names:
        listing: str = ''.join(f'- {name}\n' for name in rebuilt_names)
        text += f'\nRebuilt files:\n\n{listing}'
    return text


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
        if options.sigstore != 'off':
            names.append(ci_bundles.bundle_name(name))
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
        if _same_as_asset(asset, path, workdir):
            outcome.kept.append(path.name)
            continue
        outcome.conflicts.append(path.name)
        if options.replace_assets:
            clobber.append(path)
    return new, clobber


def _same_as_asset(
    asset: github.Asset, path: pathlib.Path, workdir: pathlib.Path
) -> bool:
    """Whether the release asset has the bytes of the local ``path``."""
    fetched: pathlib.Path = github.download_asset(
        asset, workdir / EXISTING_DIR
    )
    return common.sha256_of(fetched) == common.sha256_of(path)


def _check_ci_dists(
    release: github.Release,
    dists: list[pathlib.Path],
    workdir: pathlib.Path,
) -> None:
    """Compare the dists sign.yml uploaded with the verified PyPI bytes.

    Raises SigningError naming every asset that differs, so nothing is
    signed with GPG or listed in SHA256SUMS on the word of the workflow.
    """
    assets: dict[str, github.Asset] = {a.name: a for a in release.assets}
    differ: list[str] = [
        path.name
        for path in dists
        if not _same_as_asset(assets[path.name], path, workdir)
    ]
    if differ:
        raise signing.SigningError(
            f'{ci_sign.WORKFLOW} uploaded files that differ from PyPI: '
            + ', '.join(differ)
        )


def _needed(name: str, present: set[str], options: SignOptions) -> bool:
    """Whether an output file is missing from the release or replaceable."""
    return options.replace_assets or name not in present


SUMS_SIGNATURE: str = f'{SUMS_NAME}.asc'
RERUN_WITH_GPG: str = 'run ppt sign without --sigstore-only'
ORPHAN_ABSENT: str = (
    f'{SUMS_NAME} is missing from the release but {SUMS_SIGNATURE} is '
    f'there, so the pair needs regenerating: {RERUN_WITH_GPG}'
)
ORPHAN_STALE: str = (
    f'{SUMS_NAME} on the release is out of date and {SUMS_SIGNATURE} signs '
    f'it, so both need regenerating: {RERUN_WITH_GPG}'
)
ORPHAN_REPLACE: str = (
    f'--replace-assets would replace {SUMS_NAME} but not {SUMS_SIGNATURE}, '
    f'which signs it, so the pair needs regenerating: {RERUN_WITH_GPG}'
)
ORPHAN_UNSIGNED: str = (
    f'{SUMS_SIGNATURE} does not sign {SUMS_NAME} on the release, so the '
    f'pair needs regenerating: {RERUN_WITH_GPG}'
)


def _fetch_sums(
    assets: dict[str, github.Asset], workdir: pathlib.Path
) -> pathlib.Path:
    """Download SHA256SUMS from the release."""
    return github.download_asset(assets[SUMS_NAME], workdir / EXISTING_DIR)


def _sums_identical(
    listing: pathlib.Path, dists: list[pathlib.Path], options: SignOptions
) -> bool:
    """Whether ``listing`` has the bytes this run would write.

    ppt is the only writer of the list, so any other bytes (other files,
    digests, comments or order, a list that is not UTF-8 or has malformed
    lines) mean the list is regenerated. Nothing is parsed, so such a list
    never raises.
    """
    expected: bytes = signing.sha256sums_bytes(dists, options.rebuilt)
    return listing.read_bytes() == expected


def _sums_signed(
    listing: pathlib.Path,
    assets: dict[str, github.Asset],
    workdir: pathlib.Path,
    options: SignOptions,
) -> bool:
    """Whether SHA256SUMS.asc on the release signs ``listing``.

    An upload stopped between the two files leaves a new list with the old
    signature, which this catches.
    """
    signature: pathlib.Path = github.download_asset(
        assets[SUMS_SIGNATURE], workdir / EXISTING_DIR
    )
    return signing.gpg_verify_key(listing, signature, options.gpg_key)


def _orphan_refusal(
    release: github.Release, options: SignOptions
) -> str | None:
    """Why a run without GPG must not touch SHA256SUMS, from names alone.

    None unless GPG is off and SHA256SUMS.asc is on the release. Then
    --replace-assets or a missing list would write a list the signature
    does not sign. These need no download, so the run can refuse before
    sign.yml is dispatched or a dry run promises an upload.
    """
    names: set[str] = {a.name for a in release.assets}
    if options.gpg or SUMS_SIGNATURE not in names:
        return None
    if options.replace_assets:
        return ORPHAN_REPLACE
    if SUMS_NAME not in names:
        return ORPHAN_ABSENT
    return None


def _sums_unsigned_current(
    assets: dict[str, github.Asset],
    dists: list[pathlib.Path],
    workdir: pathlib.Path,
    options: SignOptions,
) -> bool:
    """``_sums_current`` without GPG: keep the list or regenerate it alone.

    With SHA256SUMS.asc on the release any new list would orphan that
    signature, so every case that would write the list raises
    SigningError instead, before anything is uploaded. The cases seen from
    names alone are ``_orphan_refusal``, checked first by the caller. A
    list that stays must still be the one the signature signs, which
    needs only the public key.
    """
    if SUMS_SIGNATURE not in assets:
        return (
            not options.replace_assets
            and SUMS_NAME in assets
            and _sums_identical(_fetch_sums(assets, workdir), dists, options)
        )
    listing: pathlib.Path = _fetch_sums(assets, workdir)
    if not _sums_identical(listing, dists, options):
        raise signing.SigningError(ORPHAN_STALE)
    if not _sums_signed(listing, assets, workdir, options):
        raise signing.SigningError(ORPHAN_UNSIGNED)
    return True


def _sums_current(
    release: github.Release,
    dists: list[pathlib.Path],
    workdir: pathlib.Path,
    options: SignOptions,
) -> bool:
    """Whether the SHA256SUMS pair on the release can stay as it is.

    The list and its signature are a pair: when either is missing, when
    the list differs from the one these dists get (a late wheel, a partial
    first run) or when the signature does not sign the list (an upload
    stopped between the two), both are regenerated, so the uploaded
    signature matches the uploaded list. Without GPG see
    ``_sums_unsigned_current``.
    """
    refusal: str | None = _orphan_refusal(release, options)
    if refusal is not None:
        raise signing.SigningError(refusal)
    assets: dict[str, github.Asset] = {a.name: a for a in release.assets}
    if not options.gpg:
        return _sums_unsigned_current(assets, dists, workdir, options)
    if options.replace_assets:
        return False
    if SUMS_NAME not in assets or SUMS_SIGNATURE not in assets:
        return False
    listing: pathlib.Path = _fetch_sums(assets, workdir)
    if not _sums_identical(listing, dists, options):
        return False
    return _sums_signed(listing, assets, workdir, options)


def _make_signatures(
    dists: list[pathlib.Path],
    workdir: pathlib.Path,
    options: SignOptions,
    present: set[str],
    refresh_sums: bool,
) -> tuple[list[pathlib.Path], list[pathlib.Path]]:
    """Produce only the signature and sums files the release still lacks.

    ``refresh_sums`` writes the SHA256SUMS pair (see ``_sums_current``).
    Returns the produced files and the dists whose Sigstore bundle was left
    for later (empty unless ``options.sigstore_later``).
    """
    produced: list[pathlib.Path] = []
    deferred: list[pathlib.Path] = []
    if options.gpg:
        produced.extend(
            signing.gpg_sign(p, options.gpg_key)
            for p in dists
            if _needed(f'{p.name}.asc', present, options)
        )
    if options.sigstore == 'browser':
        todo: list[pathlib.Path] = [
            p
            for p in dists
            if _needed(signing.bundle_path(p).name, present, options)
        ]
        if options.sigstore_later:
            deferred = todo
        elif todo:
            produced.extend(signing.sigstore_sign(todo))
    if refresh_sums:
        sums: pathlib.Path = signing.write_sha256sums(
            workdir, dists, options.rebuilt
        )
        produced.append(sums)
        if options.gpg:
            produced.append(signing.gpg_sign(sums, options.gpg_key))
    return produced, deferred


def _stage_pending(
    deferred: list[pathlib.Path], directory: pathlib.Path
) -> list[pathlib.Path]:
    """Copy dists that still need a bundle out of the per-release dir."""
    directory.mkdir(parents=True, exist_ok=True)
    staged: list[pathlib.Path] = []
    for path in deferred:
        target: pathlib.Path = directory / path.name
        shutil.copy2(path, target)
        staged.append(target)
    return staged


def _split_signatures(
    produced: list[pathlib.Path],
    present: set[str],
    dist_names: list[str],
    options: SignOptions,
    outcome: SignOutcome,
) -> tuple[list[pathlib.Path], list[pathlib.Path]]:
    """Produced files to upload plainly and with clobber, plus kept names."""
    new: list[pathlib.Path] = [p for p in produced if p.name not in present]
    pair: set[str] = {SUMS_NAME, SUMS_SIGNATURE}
    clobber: list[pathlib.Path] = [
        p
        for p in produced
        if p.name in present and (options.replace_assets or p.name in pair)
    ]
    replaced: set[str] = {p.name for p in clobber}
    # Until _upload runs, outcome.uploaded holds only what sign.yml added.
    outcome.kept.extend(
        name
        for name in _signature_names(dist_names, options)
        if name in present
        and name not in replaced
        and name not in outcome.uploaded
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
    by_workflow: bool = False,
) -> None:
    if FOOTER_MARKER in release.body:
        return
    text: str = footer(options, rebuilt_names, by_workflow)
    body: str = (
        f'{release.body.rstrip()}\n\n{text}' if release.body.strip() else text
    )
    github.edit_notes(options.repo, release.tag, body)


def _finish(outcome: SignOutcome) -> SignOutcome:
    if outcome.pending_bundles:
        outcome.status = PENDING
    elif outcome.uploaded:
        outcome.status = 'signed'
    else:
        outcome.status = 'already signed'
    return outcome


def finish_pending(
    release: github.Release,
    outcome: SignOutcome,
    rebuilt_names: list[str],
    options: SignOptions,
) -> SignOutcome:
    """Upload the deferred Sigstore bundles of one release, then the footer.

    The caller has run ``signing.sigstore_sign`` on
    ``outcome.pending_bundles``, so each bundle sits next to its copy. The
    footer waits until now because it promises a bundle for every file.
    """
    try:
        bundles: list[pathlib.Path] = [
            signing.bundle_path(path) for path in outcome.pending_bundles
        ]
        missing: list[str] = [b.name for b in bundles if not b.exists()]
        if missing:
            raise signing.SigningError(
                'sigstore wrote no bundle for ' + ', '.join(missing)
            )
        _upload_bundles(release, bundles, options, outcome)
        _append_footer(release, rebuilt_names, options)
    except HANDLED_ERRORS as error:
        outcome.status = 'failed'
        outcome.reason = str(error)
        return outcome
    outcome.pending_bundles = []
    return _finish(outcome)


def _upload_bundles(
    release: github.Release,
    bundles: list[pathlib.Path],
    options: SignOptions,
    outcome: SignOutcome,
) -> None:
    """Upload bundles, replacing an asset of the same name."""
    present: set[str] = {a.name for a in release.assets}
    new: list[pathlib.Path] = [b for b in bundles if b.name not in present]
    clobber: list[pathlib.Path] = [b for b in bundles if b.name in present]
    _upload(release, new, clobber, options, outcome)


def salvage_pending(
    release: github.Release,
    outcome: SignOutcome,
    options: SignOptions,
    error: str,
) -> SignOutcome:
    """Upload the deferred bundles a stopped signer run did write.

    Only a bundle whose digest covers its file goes up. The release still
    misses a bundle, so the footer stays off and the outcome is failed.
    The next run keeps the uploaded bundles as assets and signs only the
    files still without one, so nothing is signed twice.
    """
    reason: str = f'sigstore failed: {error}'
    outcome.status = 'failed'
    outcome.reason = reason
    covered: list[pathlib.Path] = [
        path for path in outcome.pending_bundles if signing.bundle_covers(path)
    ]
    if not covered:
        return outcome
    try:
        bundles: list[pathlib.Path] = [
            signing.bundle_path(path) for path in covered
        ]
        _upload_bundles(release, bundles, options, outcome)
    except HANDLED_ERRORS as upload_error:
        outcome.reason = (
            f'{reason}, uploading its bundles failed: {upload_error}'
        )
        return outcome
    outcome.reason = (
        f'{len(covered)} of {len(outcome.pending_bundles)} bundles uploaded, '
        f'{reason}'
    )
    outcome.pending_bundles = [
        path for path in outcome.pending_bundles if path not in covered
    ]
    return outcome


def _dry_run(
    release: github.Release,
    files: list[pypi.PypiFile] | list[pathlib.Path],
    options: SignOptions,
    outcome: SignOutcome,
) -> SignOutcome:
    """Print every action of a run and perform none.

    Raises SigningError when sign.yml may not sign the release.
    """
    present: set[str] = {a.name for a in release.assets}
    dist_names: list[str] = _dist_names(files)
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
    if options.sigstore == 'ci':
        wanted = _dry_run_ci(release, files, wanted, options, outcome)
    for name in wanted:
        if name not in present or options.replace_assets:
            _say(f'would upload {name}')
            outcome.uploaded.append(name)
        else:
            outcome.kept.append(name)
    if FOOTER_MARKER not in release.body:
        _say('would append footer')
    return _finish(outcome)


def _dry_run_ci(
    release: github.Release,
    files: list[pypi.PypiFile] | list[pathlib.Path],
    wanted: list[str],
    options: SignOptions,
    outcome: SignOutcome,
) -> list[str]:
    """Say what sign.yml would do, return the names left for ppt sign.

    Bundles are never uploaded by ppt sign in mode 'ci', and a dispatched
    sign.yml uploads the missing dists as well. Bundles do not get checked
    here, because a dry run downloads nothing. Raises SigningError when
    sign.yml may not sign the release.
    """
    present: set[str] = {a.name for a in release.assets}
    dist_names: list[str] = _dist_names(files)
    bundles: set[str] = {ci_bundles.bundle_name(n) for n in dist_names}
    outcome.kept.extend(n for n in wanted if n in bundles and n in present)
    left: list[str] = [n for n in wanted if n not in bundles]
    missing: list[str] = [
        n for n in dist_names if ci_bundles.bundle_name(n) not in present
    ]
    if not missing:
        return left
    if options.ci_workflow is not None:
        reason: str | None = ci_bundles.refusal(
            release, _is_rebuilt(files), options.ci_workflow
        )
        if reason is not None:
            raise signing.SigningError(reason)
    _say(f'would dispatch {ci_sign.WORKFLOW} for {", ".join(missing)}')
    return [n for n in left if n not in dist_names]


def _dist_names(files: list[pypi.PypiFile] | list[pathlib.Path]) -> list[str]:
    return [
        item.name if isinstance(item, pathlib.Path) else item.filename
        for item in files
    ]


def _is_rebuilt(files: list[pypi.PypiFile] | list[pathlib.Path]) -> bool:
    """Whether any file was built here, which sign.yml cannot sign."""
    return any(isinstance(item, pathlib.Path) for item in files)


def _failed(outcome: SignOutcome, reason: str) -> SignOutcome:
    outcome.status = 'failed'
    outcome.reason = reason
    return outcome


def sign_release(
    release: github.Release,
    files: list[pypi.PypiFile] | list[pathlib.Path],
    workdir: pathlib.Path,
    options: SignOptions,
    version: str = '',
) -> SignOutcome:
    """Sign one release, never raising for the handled failure kinds.

    Works in ``workdir / release.tag`` and removes it afterwards.
    ``version`` is the PyPI version of ``files``, which sign.yml fetches
    the files of, '' when unknown (sign.yml then takes the tag without
    its leading v).
    """
    outcome: SignOutcome = SignOutcome(
        tag=release.tag,
        version=version or versions.normalise(release.tag),
        uploaded=[],
        kept=[],
        conflicts=[],
        status='',
        reason='',
    )
    if not files:
        return _failed(outcome, 'no files for this version')
    if options.dry_run:
        try:
            return _dry_run(release, files, options, outcome)
        except HANDLED_ERRORS as error:
            return _failed(outcome, str(error))
    release_dir: pathlib.Path = workdir / release.tag
    try:
        release_dir.mkdir(parents=True, exist_ok=True)
        if not _sign_files(
            release, files, release_dir, workdir, options, outcome, version
        ):
            return outcome
    except HANDLED_ERRORS as error:
        return _failed(outcome, str(error))
    finally:
        shutil.rmtree(release_dir, ignore_errors=True)
    return _finish(outcome)


def _sign_files(
    release: github.Release,
    files: list[pypi.PypiFile] | list[pathlib.Path],
    release_dir: pathlib.Path,
    workdir: pathlib.Path,
    options: SignOptions,
    outcome: SignOutcome,
    version: str,
) -> bool:
    """The steps of ``sign_release``, False when conflicts stop it.

    In mode 'ci' the bundles are settled first, so a failing sign.yml
    leaves the release without any file from this run. Deferred bundles
    are staged in ``workdir / 'bundles' / tag``, which outlives
    ``release_dir``, and the footer then waits for them.
    """
    refusal: str | None = _orphan_refusal(release, options)
    if refusal is not None:
        raise signing.SigningError(refusal)
    dists: list[pathlib.Path] = _local_dists(files, release_dir)
    new, clobber = _compare_dists(
        release, dists, release_dir, options, outcome
    )
    if outcome.conflicts and not options.replace_assets:
        _failed(
            outcome,
            'conflicting assets left alone: ' + ', '.join(outcome.conflicts),
        )
        return False
    by_workflow: bool = False
    if options.sigstore == 'ci':
        release, new, by_workflow = _take_ci_bundles(
            release, files, dists, new, release_dir, options, outcome, version
        )
    present: set[str] = {a.name for a in release.assets}
    refresh: bool = not _sums_current(release, dists, release_dir, options)
    produced, deferred = _make_signatures(
        dists, release_dir, options, present, refresh
    )
    sig_new, sig_clobber = _split_signatures(
        produced, present, [d.name for d in dists], options, outcome
    )
    _upload(release, new + sig_new, clobber + sig_clobber, options, outcome)
    rebuilt_names: list[str] = [
        f.name for f in files if isinstance(f, pathlib.Path)
    ]
    if deferred:
        outcome.pending_bundles = _stage_pending(
            deferred, workdir / BUNDLES_DIR / release.tag
        )
    else:
        _append_footer(release, rebuilt_names, options, by_workflow)
    return True


def _take_ci_bundles(
    release: github.Release,
    files: list[pypi.PypiFile] | list[pathlib.Path],
    dists: list[pathlib.Path],
    new: list[pathlib.Path],
    release_dir: pathlib.Path,
    options: SignOptions,
    outcome: SignOutcome,
    version: str,
) -> tuple[github.Release, list[pathlib.Path], bool]:
    """Have sign.yml supply the missing bundles before anything is signed.

    Returns the release as it stands afterwards, the dists ppt sign still
    has to upload and whether sign.yml made every bundle of the release
    (see ``ci_bundles.Settled``). The dists and bundles sign.yml added count as
    uploaded by this run, and the dists it added must match the local
    PyPI bytes (see ``_check_ci_dists``). sign.yml fetches the files of
    ``version`` from PyPI (see ``sign_release``).
    """
    workflow: ci_bundles.Workflow = (
        options.ci_workflow or ci_bundles.check_workflow(options.repo)
    )
    target: ci_bundles.Target = ci_bundles.Target(
        release=release,
        dists=dists,
        rebuilt=_is_rebuilt(files),
        conflicts=list(outcome.conflicts),
        version=version,
    )
    settled: ci_bundles.Settled = ci_bundles.ensure(
        target, release_dir, options.repo, options.package, workflow
    )
    fresh: github.Release | None = settled.release
    if fresh is None:
        return release, new, settled.by_workflow
    before: set[str] = {a.name for a in release.assets}
    ours: set[str] = {d.name for d in dists} | {
        ci_bundles.bundle_name(d.name) for d in dists
    }
    outcome.uploaded.extend(
        a.name for a in fresh.assets if a.name in ours and a.name not in before
    )
    after: set[str] = {a.name for a in fresh.assets}
    _check_ci_dists(
        fresh,
        [d for d in dists if d.name in after and d.name not in before],
        release_dir,
    )
    return fresh, [p for p in new if p.name not in after], settled.by_workflow


def releases_missing_signatures(
    repo: str, project: pypi.Project
) -> list[github.Release]:
    """Releases of a PyPI version where any file lacks a signature.

    A file needs both its ``.asc`` and its ``.sigstore.json``.
    """
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
        wanted: set[str] = {
            name
            for f in files
            for name in (
                f'{f.filename}.asc',
                f'{f.filename}{signing.BUNDLE_SUFFIX}',
            )
        }
        if not wanted <= names:
            missing.append(release)
    return missing


def _version_for(tag: str, project: pypi.Project) -> str | None:
    wanted: str = versions.normalise(tag)
    for version in project.releases:
        if versions.normalise(version) == wanted:
            return version
    return None


def _with_lagging_version(
    project: pypi.Project, package: str, tag: str
) -> pypi.Project:
    """The project with the version of ``tag``, even when its JSON lags.

    Right after an upload the project document can still miss the new
    version while the document of the version has its files, so a tag
    that matches no listed version is looked up there as well. A 404 there
    leaves the project as it is, any other failure raises
    common.FetchError.
    """
    if _version_for(tag, project) is not None:
        return project
    found: tuple[str, list[pypi.PypiFile]] | None = pypi.version_files(
        package, versions.normalise(tag)
    )
    if found is None:
        return project
    version, files = found
    return dataclasses.replace(
        project, releases={**project.releases, version: files}
    )


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
    parser.add_argument(
        '--sigstore-browser',
        action='store_true',
        help='sign with Sigstore here through a browser login instead of '
        'taking the bundles from sign.yml',
    )
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
    return sign_release(
        release, pypi.files_for(project, version), workdir, options, version
    )


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


def _sigstore_mode(args: argparse.Namespace) -> SigstoreMode:
    if args.gpg_only:
        return 'off'
    if args.sigstore_browser:
        return 'browser'
    return 'ci'


def _options(args: argparse.Namespace, repo: str, package: str) -> SignOptions:
    return SignOptions(
        repo=repo,
        package=package,
        gpg=not args.sigstore_only,
        sigstore=_sigstore_mode(args),
        gpg_key=args.gpg_key,
        replace_assets=args.replace_assets,
        dry_run=args.dry_run,
    )


def main(argv: list[str]) -> int:
    """Run ``ppt sign`` and return its exit status.

    In mode 'ci' sign.yml is looked up once, before the first release, so
    a repository without it exits with EXIT_NO_WORKFLOW and nothing
    uploaded.
    """
    parser: argparse.ArgumentParser = _build_parser()
    args: argparse.Namespace = parser.parse_args(argv)
    if args.gpg_only and args.sigstore_browser:
        parser.error('--sigstore-browser cannot be used with --gpg-only')
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
        options: SignOptions = _options(args, repo, package)
        targets: list[github.Release] = _resolve_targets(
            args, options, project
        )
        if not args.all:
            project = _with_lagging_version(project, package, args.tag)
        if options.sigstore == 'ci' and targets:
            options.ci_workflow = ci_bundles.check_workflow(repo)
    except NoWorkflowError as error:
        _fail(str(error))
        return EXIT_NO_WORKFLOW
    except (
        LookupError,
        RuntimeError,
        signing.SigningError,
        common.FetchError,
    ) as error:
        _fail(str(error))
        return 2
    outcomes: list[SignOutcome] = _sign_all(targets, project, options)
    ok: bool = all(o.status in ('signed', 'already signed') for o in outcomes)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
