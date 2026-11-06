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
import sys
import tarfile
import tempfile
import typing

from python_project_tools import common, github, pypi, signing, versions

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
WORKFLOW_DIR: str = '.github/workflows'
# The top-level ``on`` key of a workflow and its inline value.
ON_LINE: re.Pattern[str] = re.compile(r"""^(?:on|"on"|'on'):(?:\s+(.*))?$""")
# One ``key: value`` line of a block map, key optionally quoted.
KEY_LINE: re.Pattern[str] = re.compile(
    r"""^["']?([\w-]+)["']?:(?:\s+(.*))?$"""
)
FLOW_KEY: re.Pattern[str] = re.compile(r'([\w-]+)["\']?\s*:')
WORD: re.Pattern[str] = re.compile(r'[\w-]+')
COMMENT: re.Pattern[str] = re.compile(r'(?:^|\s)#.*$')
TAG_FILTERS: frozenset[str] = frozenset({'tags', 'tags-ignore'})
BRANCH_FILTERS: frozenset[str] = frozenset({'branches', 'branches-ignore'})
# Event name to the keys of its map: None when the value is no block map
# (a list entry, a scalar or a flow value read loosely).
Events: typing.TypeAlias = dict[str, set[str] | None]


@dataclasses.dataclass
class Candidate:
    """A commit compared against the sdist."""

    sha: str
    committed: dt.datetime
    matched: int  # tracked files byte-identical to the sdist
    differing: list[str]  # tracked files whose bytes differ


def _git(checkout: pathlib.Path, *args: str) -> str:
    """Run git in the checkout and return stdout, raising on failure."""
    # quotepath=off keeps non-ASCII paths unquoted so they match sdist names.
    done = common.run(['git', '-c', 'core.quotepath=off', *args], cwd=checkout)
    if done.returncode != 0:
        raise RuntimeError(f'git {" ".join(args)}: {done.stderr.strip()}')
    return done.stdout


def _blob(checkout: pathlib.Path, sha: str, path: str) -> bytes:
    """The bytes of a file at a commit, undecoded so binaries survive."""
    return common.run_bytes(
        ['git', 'cat-file', 'blob', f'{sha}:{path}'], cwd=checkout
    )


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
        if any(part.endswith('.egg-info') for part in relative.parts):
            continue
        names.append(str(relative))
    return names


def _history_paths(checkout: pathlib.Path) -> set[str]:
    """Every path any commit of any ref has touched."""
    listing: str = _git(checkout, 'log', '--all', '--name-only', '--format=')
    return {line for line in listing.splitlines() if line}


def compare(
    checkout: pathlib.Path, sha: str, sdist: pathlib.Path
) -> Candidate:
    """Compare every file of the sdist with the repository at ``sha``.

    A file tracked at ``sha`` matches when its bytes are identical. A file
    not tracked at ``sha`` that some other commit does track counts as
    differing: the release was cut from a commit that had it. GENERATED
    names are skipped only when ``sha`` does not track them, and files no
    commit ever tracked (build output) are skipped.
    """
    tracked: set[str] = set(
        _git(checkout, 'ls-tree', '-r', '--name-only', sha).splitlines()
    )
    history: set[str] = _history_paths(checkout)
    matched: int = 0
    differing: list[str] = []
    with tempfile.TemporaryDirectory() as work:
        top: pathlib.Path = _extract(sdist, pathlib.Path(work))
        for name in _sdist_files(top):
            if name not in tracked:
                if name not in GENERATED and name in history:
                    differing.append(name)
                continue
            if _blob(checkout, sha, name) == (top / name).read_bytes():
                matched += 1
            else:
                differing.append(name)
    stamp: str = _git(checkout, 'show', '-s', '--format=%cI', sha).strip()
    return Candidate(
        sha=sha,
        committed=common.parse_iso_datetime(stamp),
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


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(' '))


def _clean_lines(text: str) -> list[str]:
    """Lines without comments and trailing blanks, blank lines dropped."""
    lines: list[str] = [
        COMMENT.sub('', line).rstrip() for line in text.split('\n')
    ]
    return [line for line in lines if line.strip()]


def _on_value(lines: list[str]) -> tuple[str, list[str]] | None:
    """The inline value of the top-level ``on`` key and the lines below it.

    A block sequence may sit at the same indent as ``on`` itself, so lines
    starting with ``-`` belong to it too.
    """
    for index, line in enumerate(lines):
        found: re.Match[str] | None = ON_LINE.match(line)
        if found is None:
            continue
        body: list[str] = []
        for other in lines[index + 1 :]:
            if _indent(other) == 0 and not other.startswith('-'):
                break
            body.append(other)
        return (found.group(1) or '').strip(), body
    return None


def _block_keys(body: list[str]) -> dict[str, tuple[str, list[str]]]:
    """Keys at the first indent of a block, each with value and nested lines.

    A list entry (``- push``) becomes a key with no value.
    """
    keys: dict[str, tuple[str, list[str]]] = {}
    if not body:
        return keys
    level: int = _indent(body[0])
    current: list[str] | None = None
    for line in body:
        if _indent(line) > level:
            if current is not None:
                current.append(line)
            continue
        item: str = line.strip()
        if item.startswith('-'):
            item = item[1:].strip().strip('"\'')
            keys[item] = ('', [])
            current = None
            continue
        found: re.Match[str] | None = KEY_LINE.match(item)
        current = None
        if found is not None:
            current = []
            keys[found.group(1)] = ((found.group(2) or '').strip(), current)
    return keys


def _value_keys(inline: str, nested: list[str]) -> set[str] | None:
    """The keys of an event's map, or None when it has no map."""
    if inline.startswith('{'):
        return set(FLOW_KEY.findall(inline))
    if inline:
        return None
    return set(_block_keys(nested))


def _on_events(text: str) -> Events | None:
    """The events of a workflow's ``on`` key, or None when there is none.

    Reads the shapes GitHub documents: ``on: push``, ``on: [a, b]``, a
    block map or block list under ``on:``, and a flow map read loosely.
    """
    value: tuple[str, list[str]] | None = _on_value(_clean_lines(text))
    if value is None:
        return None
    inline, body = value
    if inline.startswith(('[', '{')):
        flow: str = ' '.join([inline, *(line.strip() for line in body)])
        if inline.startswith('['):
            return dict.fromkeys(WORD.findall(flow))
        # Nested keys land here too, over-reporting the event set.
        return dict.fromkeys(FLOW_KEY.findall(flow))
    if inline:
        return {inline.strip('"\''): None}
    return {
        name: _value_keys(event_inline, nested)
        for name, (event_inline, nested) in _block_keys(body).items()
    }


def fires_on_tag_push(text: str) -> bool:
    """Whether a workflow runs when a tag is pushed.

    ``create`` always fires. ``push`` fires unless it filters on branches
    without also filtering on tags. A file whose ``on`` key cannot be found
    counts as firing, since being wrong here publishes a release.
    """
    events: Events | None = _on_events(text)
    if events is None:
        return True
    if 'create' in events:
        return True
    if 'push' not in events:
        return False
    keys: set[str] | None = events['push']
    if keys is None or keys & TAG_FILTERS:
        return True
    return not keys & BRANCH_FILTERS


def _workflow_texts(checkout: pathlib.Path, sha: str) -> list[tuple[str, str]]:
    """``(path, text)`` of every workflow file at ``sha``."""
    listing: str = _git(
        checkout, 'ls-tree', '-r', '--name-only', sha, '--', WORKFLOW_DIR
    )
    return [
        (path, _blob(checkout, sha, path).decode(errors='replace'))
        for path in listing.splitlines()
        if path.endswith(WORKFLOW_SUFFIXES)
    ]


def workflow_with_tag_trigger(checkout: pathlib.Path, sha: str) -> list[str]:
    """File names of the workflows at ``sha`` that run on a pushed tag."""
    return [
        pathlib.PurePosixPath(path).name
        for path, text in _workflow_texts(checkout, sha)
        if fires_on_tag_push(text)
    ]


def workflow_with_release_trigger(
    checkout: pathlib.Path, sha: str
) -> list[str]:
    """Paths of the workflows at ``sha`` that run on a release event.

    GitHub runs a release workflow from the tagged commit, so this is the
    file that would fire, whatever the default branch says now. Paths
    (``.github/workflows/x.yml``) so they can be matched against the
    workflow states GitHub reports.
    """
    return [
        path
        for path, text in _workflow_texts(checkout, sha)
        if github._triggers_on_release(text)
    ]


def create_tag(
    checkout: pathlib.Path,
    tag: str,
    sha: str,
    message: str,
    key: str = signing.DEFAULT_GPG_KEY,
) -> None:
    """Create an annotated tag at ``sha``, signed with ``key``."""
    done = common.run(
        ['git', 'tag', '-s', '-u', key, tag, sha, '-m', message],
        cwd=checkout,
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
    parser.add_argument('--gpg-key', default=signing.DEFAULT_GPG_KEY)
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
    existing = common.run(
        ['git', 'rev-parse', '-q', '--verify', f'refs/tags/{tag}^{{commit}}'],
        cwd=args.checkout,
    )
    if existing.returncode == 0:
        say(
            f'tag {tag} already exists at {existing.stdout.strip()}',
            file=sys.stderr,
        )
        return 1
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
    create_tag(args.checkout, tag, best.sha, message, args.gpg_key)
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
