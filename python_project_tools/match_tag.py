"""Find the commit behind an untagged PyPI release and tag it.

The sdist is the evidence: a commit matches when every file it tracks that
also appears in the sdist is byte-identical. Among the matches the commit
closest to the upload time wins. Call ``common.run`` through the module so
tests can replace it.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import pathlib
import re
import subprocess
import sys
import tarfile
import tempfile
import typing

from python_project_tools import common, pypi, versions

VERSION_FILES: tuple[str, ...] = (
    'setup.py',
    'setup.cfg',
    'pyproject.toml',
    '__init__.py',
    '__about__.py',
    '__version__.py',
)
# Written by the build backend, so they say nothing about the commit.
GENERATED: tuple[str, ...] = (
    'PKG-INFO',
    'setup.cfg',
    'MANIFEST',
    'MANIFEST.in',
)

TAG_MESSAGE: str = (
    'Retroactive tag for PyPI release {package} {version}.\n\n'
    'Matched against {filename} (sha256 {sha256}) on {date}: every tracked '
    'file in the sdist is byte-identical to this commit.'
)
WORKFLOW_SUFFIXES: tuple[str, ...] = ('.yml', '.yaml')
TAGS_LINE: re.Pattern[str] = re.compile(r'^\s*tags:', re.MULTILINE)
PUSH_LINE: re.Pattern[str] = re.compile(r'^\s*push:', re.MULTILINE)


@dataclasses.dataclass
class Candidate:
    """A commit compared against the sdist."""

    sha: str
    committed: dt.datetime
    matched: int  # tracked files byte-identical to the sdist
    differing: list[str]  # tracked files whose bytes differ


def _git(checkout: pathlib.Path, *args: str) -> str:
    """Run git in the checkout and return stdout, raising on failure."""
    done = common.run(['git', *args], cwd=checkout)
    if done.returncode != 0:
        raise RuntimeError(f'git {" ".join(args)}: {done.stderr.strip()}')
    return done.stdout


def _blob(checkout: pathlib.Path, sha: str, path: str) -> bytes:
    """The bytes of a file at a commit.

    common.run decodes output as text, which would corrupt binary files, so
    this one helper calls subprocess with the same argv shape.
    """
    done: subprocess.CompletedProcess[bytes] = subprocess.run(
        ['git', 'cat-file', 'blob', f'{sha}:{path}'],
        capture_output=True,
        cwd=checkout,
    )
    if done.returncode != 0:
        raise RuntimeError(f'git cat-file {sha}:{path}: {done.stderr!r}')
    return done.stdout


def _version_paths(checkout: pathlib.Path) -> list[str]:
    """Version file paths that exist anywhere in history."""
    listing: str = _git(
        checkout,
        'log',
        '--all',
        '--format=',
        '--name-only',
        '--diff-filter=AM',
    )
    found: dict[str, None] = {}
    for line in listing.splitlines():
        if line and pathlib.PurePosixPath(line).name in VERSION_FILES:
            found[line] = None
    return list(found)


def candidates(checkout: pathlib.Path, version: str) -> list[str]:
    """Commits whose diff adds or removes the version string, newest first."""
    paths: list[str] = _version_paths(checkout)
    if not paths:
        return []
    listing: str = _git(
        checkout, 'log', '--all', '--format=%H', f'-S{version}', '--', *paths
    )
    return list(dict.fromkeys(listing.split()))


def _extract(sdist: pathlib.Path, into: pathlib.Path) -> pathlib.Path:
    """Unpack an sdist and return its single top-level directory."""
    if not hasattr(tarfile, 'data_filter'):
        raise RuntimeError(
            'match-tag needs tarfile.data_filter (Python 3.10.12, 3.11.4 or '
            'later) to unpack sdists safely'
        )
    with tarfile.open(sdist) as tar:
        tar.extractall(into, filter='data')
    tops: list[pathlib.Path] = [p for p in into.iterdir() if p.is_dir()]
    if len(tops) != 1:
        raise RuntimeError(f'{sdist.name}: expected one top-level directory')
    return tops[0]


def _sdist_files(top: pathlib.Path) -> list[str]:
    """Relative posix paths of the sdist files that say something."""
    names: list[str] = []
    for path in sorted(top.rglob('*')):
        if not path.is_file():
            continue
        relative: pathlib.PurePosixPath = pathlib.PurePosixPath(
            path.relative_to(top).as_posix()
        )
        if str(relative) in GENERATED:
            continue
        if any(part.endswith('.egg-info') for part in relative.parts):
            continue
        names.append(str(relative))
    return names


def compare(
    checkout: pathlib.Path, sha: str, sdist: pathlib.Path
) -> Candidate:
    """Compare every tracked file of the sdist with its bytes at ``sha``."""
    tracked: set[str] = set(
        _git(checkout, 'ls-tree', '-r', '--name-only', sha).splitlines()
    )
    matched: int = 0
    differing: list[str] = []
    with tempfile.TemporaryDirectory() as work:
        top: pathlib.Path = _extract(sdist, pathlib.Path(work))
        for name in _sdist_files(top):
            if name not in tracked:
                continue
            if _blob(checkout, sha, name) == (top / name).read_bytes():
                matched += 1
            else:
                differing.append(name)
    stamp: str = _git(checkout, 'show', '-s', '--format=%cI', sha).strip()
    return Candidate(
        sha=sha,
        committed=dt.datetime.fromisoformat(stamp),
        matched=matched,
        differing=differing,
    )


def choose(cands: list[Candidate], uploaded: dt.datetime) -> Candidate | None:
    """The full match committed closest to the upload, or None."""
    full: list[Candidate] = [
        c for c in cands if not c.differing and c.matched > 0
    ]
    if not full:
        return None
    return min(full, key=lambda c: abs(c.committed - uploaded))


def workflow_with_tag_trigger(checkout: pathlib.Path, sha: str) -> list[str]:
    """Workflow files at ``sha`` that run on a pushed tag."""
    listing: str = _git(
        checkout,
        'ls-tree',
        '-r',
        '--name-only',
        sha,
        '--',
        '.github/workflows',
    )
    found: list[str] = []
    for path in listing.splitlines():
        if not path.endswith(WORKFLOW_SUFFIXES):
            continue
        text: str = _blob(checkout, sha, path).decode(errors='replace')
        if PUSH_LINE.search(text) and TAGS_LINE.search(text):
            found.append(pathlib.PurePosixPath(path).name)
    return found


def create_tag(
    checkout: pathlib.Path, tag: str, sha: str, message: str
) -> None:
    """Create an annotated, signed tag at ``sha``."""
    done = common.run(
        ['git', 'tag', '-s', tag, sha, '-m', message], cwd=checkout
    )
    if done.returncode != 0:
        raise RuntimeError(f'git tag {tag}: {done.stderr.strip()}')


def push_tag(checkout: pathlib.Path, tag: str) -> None:
    """Push one tag to origin."""
    done = common.run(['git', 'push', 'origin', tag], cwd=checkout)
    if done.returncode != 0:
        raise RuntimeError(f'git push origin {tag}: {done.stderr.strip()}')


def _parser() -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog='ppt match-tag',
        description='Find the commit behind a PyPI release and tag it.',
        allow_abbrev=False,
    )
    parser.add_argument('package')
    parser.add_argument('version')
    parser.add_argument(
        '--checkout', type=pathlib.Path, default=pathlib.Path()
    )
    parser.add_argument('--create', action='store_true')
    parser.add_argument('--push', action='store_true')
    parser.add_argument('--allow-workflow-trigger', action='store_true')
    return parser


def say(text: str, file: typing.TextIO | None = None) -> None:
    """Print one line, to stdout unless a stream is given."""
    print(text, file=file)  # noqa: T201


def _print_table(cands: list[Candidate]) -> None:
    say(f'{"sha":40}  {"committed":25}  matched  differing')
    for cand in cands:
        say(
            f'{cand.sha:40}  {cand.committed.isoformat():25}  '
            f'{cand.matched:7}  {len(cand.differing)}'
        )


def _find_sdist(package: str, version: str) -> pypi.PypiFile | None:
    project: pypi.Project = pypi.project(package)
    for file in pypi.files_for(project, version):
        if file.packagetype == 'sdist':
            return file
    return None


def _tag_and_push(
    args: argparse.Namespace, best: Candidate, sdist: pypi.PypiFile
) -> int:
    tag: str = versions.tag_for(args.version)
    if args.push:
        flows: list[str] = workflow_with_tag_trigger(args.checkout, best.sha)
        if flows and not args.allow_workflow_trigger:
            say(
                f'refusing to push {tag}: {", ".join(flows)} runs on pushed '
                'tags and could publish. Pass --allow-workflow-trigger to '
                'push anyway.',
                file=sys.stderr,
            )
            return 2
    message: str = TAG_MESSAGE.format(
        package=args.package,
        version=args.version,
        filename=sdist.filename,
        sha256=sdist.sha256,
        date=dt.date.today().isoformat(),
    )
    create_tag(args.checkout, tag, best.sha, message)
    say(f'created signed tag {tag} at {best.sha}')
    if args.push:
        push_tag(args.checkout, tag)
        say(f'pushed {tag}')
    return 0


def main(argv: list[str]) -> int:
    """Run ``ppt match-tag`` and return its exit status."""
    args: argparse.Namespace = _parser().parse_args(argv)
    try:
        sdist: pypi.PypiFile | None = _find_sdist(args.package, args.version)
        if sdist is None:
            say(f'{args.package} {args.version} has no sdist on PyPI')
            return 1
        with tempfile.TemporaryDirectory() as work:
            archive: pathlib.Path = pypi.download(sdist, pathlib.Path(work))
            cands: list[Candidate] = [
                compare(args.checkout, sha, archive)
                for sha in candidates(args.checkout, args.version)
            ]
        _print_table(cands)
        best: Candidate | None = choose(cands, sdist.upload_time)
        if best is None:
            say(f'no commit matches {args.version}')
            return 1
        say(f'match: {best.sha}')
        if not args.create:
            return 0
        return _tag_and_push(args, best, sdist)
    except (
        LookupError,
        common.FetchError,
        pypi.IntegrityError,
        RuntimeError,
    ) as error:
        say(f'match-tag failed: {error}', file=sys.stderr)
        return 1
