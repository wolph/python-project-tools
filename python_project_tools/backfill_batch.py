"""The ``ppt backfill`` command line, for one package or a batch of them.

``ppt backfill PACKAGE`` backfills one package. ``ppt backfill --batch
FILE`` backfills every package the file lists, one ``PACKAGE
CHECKOUT_PATH`` pair per line. Each package runs its preflight, plan and
loop in turn, then the Sigstore bundles of every package are made in one
signer run, so the whole batch needs one browser login. Then each
package marks its Latest target and prints its report, in input order. A
package that is refused or stops does not stop the batch, and the releases
it created still get their bundles. The single package form is the batch
of one without the summary table. Call ``backfill`` through its attributes
so tests can replace them.
"""

from __future__ import annotations

import argparse
import dataclasses
import pathlib
import sys

from packaging.utils import canonicalize_name

from python_project_tools import backfill, common, pypi, signing

COMMENT: str = '#'
BATCH_LINE: str = 'PACKAGE CHECKOUT_PATH'
# Options that name one package and so make no sense for a batch.
SINGLE_ONLY: tuple[str, ...] = ('only', 'repo', 'checkout')
SUMMARY_COLUMNS: tuple[str, ...] = (
    'package',
    'exit',
    'rows signed',
    'rows failed',
    'Latest',
)
SIGNED_STATUSES: tuple[str, ...] = ('signed', 'already signed')
# What a package can raise before or during its loop without ending the
# batch, the same kinds ``ppt backfill PACKAGE`` turns into exit 2.
PACKAGE_ERRORS: tuple[type[Exception], ...] = (
    LookupError,
    RuntimeError,
    common.FetchError,
)


class BatchFileError(ValueError):
    """The batch file cannot be read or holds a line that is not valid."""


@dataclasses.dataclass
class BatchEntry:
    """One package of a batch and the checkout of its repository."""

    package: str
    checkout: pathlib.Path


@dataclasses.dataclass
class PackageResult:
    """What became of one package of a batch.

    ``refused`` is set when the package never started, ``run`` while it
    is in progress and ``report`` once it is finished.
    """

    entry: BatchEntry
    run: backfill.PackageRun | None = None
    report: backfill.BackfillReport | None = None
    refused: str = ''


def _say(message: str) -> None:
    print(message)  # noqa: T201


def _fail(message: str) -> None:
    print(message, file=sys.stderr)  # noqa: T201


def _parse_line(number: int, line: str) -> BatchEntry:
    """One ``PACKAGE CHECKOUT_PATH`` line, the path may hold spaces.

    The path must be absolute after ``~`` expansion and be a directory, so
    no line depends on the directory ``ppt`` runs in.
    """
    parts: list[str] = line.split(maxsplit=1)
    if len(parts) != 2:
        raise BatchFileError(
            f'line {number}: expected {BATCH_LINE}, got {line!r}'
        )
    package, path = parts
    checkout: pathlib.Path = pathlib.Path(path.strip()).expanduser()
    if not checkout.is_absolute():
        raise BatchFileError(
            f'line {number}: checkout {path!r} is not an absolute path'
        )
    if not checkout.is_dir():
        raise BatchFileError(
            f'line {number}: checkout {checkout} is not a directory'
        )
    return BatchEntry(package=package, checkout=checkout)


def parse_batch(text: str) -> list[BatchEntry]:
    """The entries of a batch file, in file order.

    Blank lines and lines starting with ``#`` are skipped. A bad line, a
    package listed twice or a file without entries raises BatchFileError,
    so a batch with a mistake starts nothing.
    """
    entries: list[BatchEntry] = []
    seen: dict[str, int] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line: str = raw.strip()
        if not line or line.startswith(COMMENT):
            continue
        entry: BatchEntry = _parse_line(number, line)
        name: str = canonicalize_name(entry.package)
        if name in seen:
            raise BatchFileError(
                f'line {number}: {entry.package} is already on line '
                f'{seen[name]}'
            )
        seen[name] = number
        entries.append(entry)
    if not entries:
        raise BatchFileError('the batch file lists no packages')
    return entries


def read_batch(path: pathlib.Path) -> list[BatchEntry]:
    """The entries of the batch file at ``path``."""
    try:
        text: str = path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as error:
        raise BatchFileError(f'cannot read {path}: {error}') from error
    return parse_batch(text)


def _options(
    args: argparse.Namespace, repo: str, checkout: pathlib.Path
) -> backfill.BackfillOptions:
    return backfill.BackfillOptions(
        repo=repo,
        checkout=checkout,
        only=args.only,
        dry_run=args.dry_run,
        replace_assets=args.replace_assets,
        gpg=not args.sigstore_only,
        sigstore=not args.gpg_only,
        gpg_key=args.gpg_key,
        allow_workflow_trigger=args.allow_workflow_trigger,
    )


def _refuse(result: PackageResult, prefix: str, *reasons: str) -> None:
    """Print why a package does not start and keep it on the result."""
    for reason in reasons:
        _fail(f'{prefix}{reason}')
    result.refused = ', '.join(reasons)


def _start(
    entry: BatchEntry, args: argparse.Namespace, prefix: str
) -> PackageResult:
    """Preflight and plan one package, creating nothing."""
    result: PackageResult = PackageResult(entry=entry)
    if not entry.checkout.is_dir():
        _refuse(
            result, prefix, f'--checkout {entry.checkout} is not a directory'
        )
        return result
    try:
        repo: str | None = args.repo or pypi.project(entry.package).repo
        if not repo:
            _refuse(
                result,
                prefix,
                f'no --repo and PyPI has none for {entry.package}',
            )
            return result
        reasons: list[str] = backfill.preflight(repo)
        if reasons:
            _refuse(result, prefix, *reasons)
            return result
        result.run = backfill.start_run(
            entry.package, _options(args, repo, entry.checkout)
        )
    except PACKAGE_ERRORS as error:
        _refuse(result, prefix, str(error))
    return result


def _execute(run: backfill.PackageRun) -> None:
    """Run the loop of one package, an escaping error stops the package.

    The run keeps its rows and its pending bundles, so the releases it
    created are still signed with the rest of the batch.
    """
    try:
        backfill.execute_run(run)
    except PACKAGE_ERRORS as error:
        run.stopped = str(error) or type(error).__name__


def run_packages(
    entries: list[BatchEntry], args: argparse.Namespace, labelled: bool
) -> list[PackageResult]:
    """Run every package, sign all their bundles at once, then finish each.

    ``labelled`` prefixes the messages of a package with its name, for a
    batch. The work directories are removed whatever happens.
    """
    results: list[PackageResult] = []
    try:
        for index, entry in enumerate(entries, start=1):
            prefix: str = f'{entry.package}: ' if labelled else ''
            if labelled:
                _say(f'== {entry.package} ({index}/{len(entries)}) ==')
            result: PackageResult = _start(entry, args, prefix)
            results.append(result)
            if result.run is not None:
                _execute(result.run)
        runs: list[backfill.PackageRun] = [
            result.run for result in results if result.run is not None
        ]
        backfill.sign_pending(runs)
        for result in results:
            if result.run is not None:
                result.report = backfill.finish_run(result.run)
    finally:
        for result in results:
            if result.run is not None:
                backfill.close_run(result.run)
    return results


def package_status(result: PackageResult) -> int:
    """The exit status ``ppt backfill PACKAGE`` would give this package."""
    if result.report is None:
        return 2
    return backfill.exit_status(result.report)


def _print_reports(results: list[PackageResult], labelled: bool) -> None:
    for result in results:
        report: backfill.BackfillReport | None = result.report
        package: str = result.entry.package
        if report is None:
            if labelled:
                _say(f'## {package}\n\nRefused: {result.refused}\n')
            continue
        if labelled:
            _say(f'## {package} ({report.repo})\n')
        _say(backfill.to_markdown(report))
        if report.stopped:
            prefix: str = f'{package}: ' if labelled else ''
            _fail(f'{prefix}{report.stopped}')


def _latest_cell(report: backfill.BackfillReport) -> str:
    before: str = report.latest_before or 'none'
    after: str = report.latest_after or 'none'
    target: str = report.latest_target or 'none'
    verdict: str = 'CHANGED' if backfill.latest_changed(report) else 'OK'
    return f'before: {before}, after: {after}, target: {target}, {verdict}'


def _summary_cells(result: PackageResult) -> list[str]:
    """Package, exit, rows signed, rows failed and the Latest line.

    Rows failed counts every row that makes the exit status 1: failed,
    skipped and still waiting for bundles.
    """
    report: backfill.BackfillReport | None = result.report
    status: str = str(package_status(result))
    if report is None:
        return [result.entry.package, status, '0', '0', result.refused]
    signed: int = 0
    failed: int = 0
    for _, outcome, _ in report.rows:
        if outcome is not None and outcome.status in SIGNED_STATUSES:
            signed += 1
        elif backfill.row_failed(outcome):
            failed += 1
    latest: str = _latest_cell(report)
    if report.stopped:
        latest = f'stopped, {latest}'
    return [result.entry.package, status, str(signed), str(failed), latest]


def summary(results: list[PackageResult]) -> str:
    """The batch summary as a Markdown table, one row per package."""
    lines: list[str] = [
        '| ' + ' | '.join(SUMMARY_COLUMNS) + ' |',
        '|' + '|'.join(' --- ' for _ in SUMMARY_COLUMNS) + '|',
    ]
    for result in results:
        cells: list[str] = [
            backfill.markdown_cell(value) for value in _summary_cells(result)
        ]
        lines.append('| ' + ' | '.join(cells) + ' |')
    return '\n'.join(lines) + '\n'


def _build_parser() -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog='ppt backfill',
        description='Put every PyPI version on a signed GitHub release.',
        allow_abbrev=False,
    )
    parser.add_argument('package', nargs='?')
    parser.add_argument(
        '--batch',
        type=pathlib.Path,
        metavar='FILE',
        help=f'one "{BATCH_LINE}" per line instead of PACKAGE',
    )
    parser.add_argument('--repo', help='OWNER/NAME, default from PyPI')
    parser.add_argument(
        '--checkout', type=pathlib.Path, help='default: the current directory'
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


def _batch_conflict(args: argparse.Namespace) -> str | None:
    """Why the arguments cannot go with --batch, or None when they can."""
    if args.package is not None:
        return '--batch takes no PACKAGE, list it in the batch file'
    for name in SINGLE_ONLY:
        if getattr(args, name) is not None:
            return f'--batch cannot be combined with --{name}'
    return None


def _main_single(args: argparse.Namespace) -> int:
    if args.package is None:
        _fail('give a PACKAGE or --batch FILE')
        return 2
    checkout: pathlib.Path = args.checkout or pathlib.Path()
    entry: BatchEntry = BatchEntry(package=args.package, checkout=checkout)
    results: list[PackageResult] = run_packages([entry], args, False)
    _print_reports(results, False)
    return package_status(results[0])


def _main_batch(args: argparse.Namespace) -> int:
    conflict: str | None = _batch_conflict(args)
    if conflict is not None:
        _fail(conflict)
        return 2
    try:
        entries: list[BatchEntry] = read_batch(args.batch)
    except BatchFileError as error:
        _fail(f'{args.batch}: {error}')
        return 2
    results: list[PackageResult] = run_packages(entries, args, True)
    _print_reports(results, True)
    _say(summary(results))
    return max(package_status(result) for result in results)


def main(argv: list[str]) -> int:
    """Run ``ppt backfill`` and return its exit status.

    0 when every package is OK, 1 when a row of any package failed, was
    skipped or Latest is off target, 2 when any package was refused or
    stopped.
    """
    args: argparse.Namespace = _build_parser().parse_args(argv)
    if args.batch is None:
        return _main_single(args)
    return _main_batch(args)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
