"""Find the commit behind an untagged PyPI release and tag it.

The sdist is the evidence: a commit matches when every file it tracks that
also appears in the sdist is byte-identical. Among the matches the commit
closest to the upload time wins. Call ``common.run`` through the module so
tests can replace it.
"""

from __future__ import annotations

import argparse
import configparser
import dataclasses
import datetime as dt
import pathlib
import re
import sys
import tarfile
import tempfile
import typing

from python_project_tools import (
    common,
    pypi,
    remote_tags,
    signing,
    versions,
    workflow_on,
)

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

# The only keys setuptools writes into the [egg_info] block of setup.cfg.
EGG_INFO_KEYS: frozenset[str] = frozenset(
    {'tag_build', 'tag_date', 'tag_svn_revision'}
)
SECTION_LINE: re.Pattern[str] = re.compile(r'^\[([^\]]*)\]')
KEY_NAME: re.Pattern[str] = re.compile(r'^([^\s=:#;][^=:]*?)\s*[=:]')

TAG_MESSAGE: str = (
    'Retroactive tag for PyPI release {package} {version}.\n\n'
    'Matched against {filename} (sha256 {sha256}) on {date}: every tracked '
    'file in the sdist is byte-identical to this commit.'
)
WORKFLOW_SUFFIXES: tuple[str, ...] = ('.yml', '.yaml')
WORKFLOW_DIR: str = '.github/workflows'
LOCAL_TAG_EXISTS: str = (
    'local tag {name} exists and is not on GitHub, not touching it'
)


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
        *remote_tags.GITHUB_GLOBS,
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
    """Commits whose diff adds or removes the version string, newest first.

    Only history GitHub has is searched: its branches and its tags, see
    remote_tags.GITHUB_GLOBS. A commit that only a local branch, the stash
    or a local tag reaches is left out, a tag pushed at one would publish
    it.
    """
    paths: list[str] = _version_paths(checkout)
    if not paths:
        return []
    listing: str = _git(
        checkout,
        'log',
        *remote_tags.GITHUB_GLOBS,
        '--format=%H',
        f'-S{version}',
        '--',
        *paths,
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
    """Every path any commit GitHub has has touched."""
    listing: str = _git(
        checkout,
        'log',
        *remote_tags.GITHUB_GLOBS,
        '--name-only',
        '--format=',
    )
    return {line for line in listing.splitlines() if line}


def _section_keys(body: list[str]) -> set[str] | None:
    """Keys set in an ini section body, or None when a line is unreadable."""
    keys: set[str] = set()
    for line in body:
        if not line.strip() or line[0] in '#;' or line[0].isspace():
            continue  # blank, comment or continuation line
        found: re.Match[str] | None = KEY_NAME.match(line)
        if found is None:
            return None
        keys.add(found.group(1).strip().lower())
    return keys


def strip_egg_info(text: str) -> str:
    """Remove the [egg_info] block setuptools appends to ``setup.cfg``.

    Only a section whose keys are all in EGG_INFO_KEYS goes, together with
    the blank lines after it. An [egg_info] section with any other key is
    real configuration and stays, as does every other section.
    """
    lines: list[str] = text.splitlines(keepends=True)
    kept: list[str] = []
    index: int = 0
    while index < len(lines):
        header: re.Match[str] | None = SECTION_LINE.match(lines[index])
        end: int = index + 1
        while (
            header and end < len(lines) and not SECTION_LINE.match(lines[end])
        ):
            end += 1
        if header and header.group(1).strip() == 'egg_info':
            keys: set[str] | None = _section_keys(lines[index + 1 : end])
            if keys is not None and keys <= EGG_INFO_KEYS:
                index = end
                continue
        kept.append(lines[index])
        index += 1
    return ''.join(kept)


def _keep_case(optionstr: str) -> str:
    """Leave option names as written, configparser lower-cases them."""
    return optionstr


def _setup_cfg_sections(text: str) -> dict[str, dict[str, str]]:
    """Parse ``setup.cfg`` text into sections without the setuptools tags.

    Key case is kept and duplicates are tolerated. The tag keys of an
    [egg_info] section are dropped, and the section with them when nothing
    else is left. Raises configparser.Error on unparsable text.
    """
    parser: configparser.RawConfigParser = configparser.RawConfigParser(
        strict=False, interpolation=None
    )
    parser.optionxform = _keep_case
    parser.read_string(text)
    defaults: dict[str, str] = dict(parser.defaults())
    sections: dict[str, dict[str, str]] = {}
    if defaults:
        sections[parser.default_section] = defaults
    for name in parser.sections():
        sections[name] = {
            key: value
            for key, value in parser.items(name)
            if key not in defaults or value != defaults[key]
        }
    egg: dict[str, str] | None = sections.get('egg_info')
    if egg is not None:
        for key in EGG_INFO_KEYS:
            egg.pop(key, None)
        if not egg:
            del sections['egg_info']
    return {
        name: {key: value.strip() for key, value in body.items()}
        for name, body in sections.items()
    }


def setup_cfg_equivalent(a: str, b: str) -> bool:
    """Whether two ``setup.cfg`` texts hold the same configuration.

    Old setuptools rewrites the file through configparser when building an
    sdist: spacing around "=" changes, blank lines go and an [egg_info] tag
    block appears. Both texts are parsed and compared as sections of
    stripped values, ignoring the [egg_info] tag keys. When either side
    does not parse, the texts are compared with strip_egg_info instead.
    """
    try:
        return _setup_cfg_sections(a) == _setup_cfg_sections(b)
    except configparser.Error:
        return strip_egg_info(a).rstrip() == strip_egg_info(b).rstrip()


def _same_bytes(name: str, tracked: bytes, built: bytes) -> bool:
    """Whether a tracked file matches its sdist copy.

    ``setup.cfg`` at the top level is compared as configuration, see
    setup_cfg_equivalent. Every other file must match byte for byte.
    """
    if tracked == built:
        return True
    if name != 'setup.cfg':
        return False
    try:
        left: str = tracked.decode('utf-8')
        right: str = built.decode('utf-8')
    except UnicodeDecodeError:
        return False
    return setup_cfg_equivalent(left, right)


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
            if _same_bytes(
                name, _blob(checkout, sha, name), (top / name).read_bytes()
            ):
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
        if workflow_on.fires_on_tag_push(text)
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
        if workflow_on.fires_on_release(text)
    ]


def create_tag(
    checkout: pathlib.Path,
    tag: str,
    sha: str,
    message: str,
    key: str = signing.DEFAULT_GPG_KEY,
) -> None:
    """Create an annotated tag at ``sha``, signed with ``key``.

    Refuses when a local tag of that name exists, whatever it points at:
    an existing tag is never moved or replaced. Refuses too when ``sha``
    is not reachable from a ref GitHub has, see remote_tags.on_github:
    pushing the tag would publish that commit and its history.
    """
    if remote_tags.local_object(checkout, tag) is not None:
        raise RuntimeError(LOCAL_TAG_EXISTS.format(name=tag))
    remote_tags.require_on_github(checkout, sha)
    done = common.run(
        ['git', 'tag', '-s', '-u', key, tag, sha, '-m', message],
        cwd=checkout,
    )
    if done.returncode != 0:
        raise RuntimeError(f'git tag {tag}: {done.stderr.strip()}')


def push_tag(checkout: pathlib.Path, tag: str) -> None:
    """Push one local tag to origin and record it as GitHub's tag.

    The push names ``refs/tags/TAG`` without a ``+``, so a tag of that
    name on origin is never overwritten and a branch of the same name
    never goes instead. The private ref is updated only after the push.
    """
    local: str = f'{remote_tags.LOCAL_TAGS}/{tag}'
    done = common.run(['git', 'push', 'origin', local], cwd=checkout)
    if done.returncode != 0:
        raise RuntimeError(f'git push origin {local}: {done.stderr.strip()}')
    remote_tags.record_pushed(checkout, tag)


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
    existing: str | None = remote_tags.commit(args.checkout, tag)
    if existing is not None:
        say(
            f'tag {tag} already exists on GitHub at {existing}',
            file=sys.stderr,
        )
        return 1
    if remote_tags.local_object(args.checkout, tag) is not None:
        say(LOCAL_TAG_EXISTS.format(name=tag), file=sys.stderr)
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
        remote_tags.fetch(args.checkout)
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
