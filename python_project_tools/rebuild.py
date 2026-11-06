"""Rebuild the files of a release from its tag.

Some PyPI versions have no files left, so the only evidence of what they
shipped is the tagged source. ``rebuild`` exports that tag with
``git archive`` into a fresh directory and runs ``uv build`` there. The tag
is GitHub's, read from ``remote_tags.REMOTE_TAGS``: a local tag of the same
name can point elsewhere. Call ``common.run`` through the module so tests
can replace it.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tarfile
import tempfile

from python_project_tools import common, remote_tags

DIST_SUFFIXES: tuple[str, ...] = ('.whl', '.tar.gz')
ARCHIVE_NAME: str = 'source.tar'
TREE_NAME: str = 'tree'


class RebuildError(Exception):
    """The tag could not be exported or built."""


def _export(
    checkout: pathlib.Path, tag: str, work: pathlib.Path
) -> pathlib.Path:
    """Export GitHub's tag into ``work / 'tree'`` and return that directory."""
    source: str = remote_tags.ref(tag)
    archive: pathlib.Path = work / ARCHIVE_NAME
    done: subprocess.CompletedProcess[str] = common.run(
        [
            *('git', 'archive', '--format=tar'),
            *('--output', str(archive)),
            source,
        ],
        cwd=checkout,
    )
    if done.returncode != 0:
        raise RebuildError(f'git archive {source}: {done.stderr.strip()}')
    tree: pathlib.Path = work / TREE_NAME
    tree.mkdir()
    if not hasattr(tarfile, 'data_filter'):
        raise RebuildError(
            'rebuild needs tarfile.data_filter (Python 3.10.12, 3.11.4 or '
            'later) to unpack the archive safely'
        )
    with tarfile.open(archive) as tar:
        tar.extractall(tree, filter='data')
    return tree


def _built(out: pathlib.Path) -> list[pathlib.Path]:
    """The wheels and sdists in a directory, sorted by name."""
    return sorted(
        path for path in out.iterdir() if path.name.endswith(DIST_SUFFIXES)
    )


def _commit_time(checkout: pathlib.Path, tag: str) -> str:
    """The commit time of GitHub's tag in seconds since the epoch, as text."""
    source: str = remote_tags.ref(tag)
    done: subprocess.CompletedProcess[str] = common.run(
        ['git', 'log', '-1', '--format=%ct', source], cwd=checkout
    )
    stamp: str = done.stdout.strip() if done.returncode == 0 else ''
    if not stamp.isdigit():
        raise RebuildError(f'git log {source}: {done.stderr.strip()}')
    return stamp


def rebuild(
    checkout: pathlib.Path, tag: str, out: pathlib.Path
) -> list[pathlib.Path]:
    """Build the wheel and sdist of a tag into ``out`` and return them.

    GitHub's tag, fetched by ``remote_tags.fetch``, is exported with
    ``git archive`` into a temporary directory that is removed afterwards.
    ``SOURCE_DATE_EPOCH`` is the tag's commit time, so backends that
    honour it write the same timestamps on every rebuild.
    Raises RebuildError when git or uv fail or when ``out`` holds no wheel
    or sdist afterwards.
    """
    out.mkdir(parents=True, exist_ok=True)
    epoch: str = _commit_time(checkout, tag)
    work: pathlib.Path = pathlib.Path(tempfile.mkdtemp(prefix='ppt-rebuild-'))
    try:
        tree: pathlib.Path = _export(checkout, tag, work)
        done: subprocess.CompletedProcess[str] = common.run(
            ['uv', 'build', '--out-dir', str(out)],
            cwd=tree,
            env={'SOURCE_DATE_EPOCH': epoch},
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if done.returncode != 0:
        raise RebuildError(f'uv build {tag}: {done.stderr.strip()}')
    built: list[pathlib.Path] = _built(out)
    if not built:
        raise RebuildError(f'uv build {tag}: no wheel or sdist in {out}')
    return built
