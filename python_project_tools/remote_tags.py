"""GitHub's tags, mirrored into a private ref namespace of the checkout.

Releases attach to the tags on GitHub, so those are the truth for
backfill. A checkout can hold local tags that point elsewhere, and a plain
``git fetch --tags`` then refuses to clobber them and fails. GitHub's tags
are fetched into ``refs/ppt/remote-tags/`` instead, with a forced refspec
that only ever writes there: ``refs/tags/`` of the checkout is never
written, moved or pruned. Every decision reads the private refs. Call
``common.run`` through the module so tests can replace it.

GitHub's branches are mirrored the same way into ``refs/remotes/origin/``.
Together with the private tag refs they are the only history a tag may be
placed on: a commit that only a local branch, the stash or a local tag
reaches is not on GitHub, and a pushed tag would publish it.
"""

from __future__ import annotations

import pathlib
import subprocess

from packaging.version import InvalidVersion, Version

from python_project_tools import common, versions

LOCAL_TAGS: str = 'refs/tags'
REMOTE_TAGS: str = 'refs/ppt/remote-tags'
REFSPEC: str = f'+{LOCAL_TAGS}/*:{REMOTE_TAGS}/*'
# --no-tags keeps git from following tags into refs/tags. With a refspec on
# the command line, --prune only prunes that refspec's destination, the
# private namespace, so a tag deleted on GitHub leaves it as well. git
# already ignores fetch.pruneTags and remote.origin.pruneTags when a
# refspec is given, --no-prune-tags says so explicitly: those settings
# would add refs/tags/*:refs/tags/* and delete local tags.
FETCH: tuple[str, ...] = (
    *('git', 'fetch', '--no-tags', '--prune', '--no-prune-tags'),
    *('origin', REFSPEC),
)
# The branches of GitHub, fetched without tags. The explicit refspec does
# not depend on remote.origin.fetch, and --no-prune-tags keeps
# fetch.pruneTags from adding refs/tags/*:refs/tags/* to it.
BRANCH_REFSPEC: str = '+refs/heads/*:refs/remotes/origin/*'
FETCH_BRANCHES: tuple[str, ...] = (
    *('git', 'fetch', '--no-tags', '--prune', '--no-prune-tags'),
    *('origin', BRANCH_REFSPEC),
)
REMOTE_BRANCHES: str = 'refs/remotes/origin'
# Everything GitHub is known to have: its branches and its tags.
GITHUB_NAMESPACES: tuple[str, ...] = (REMOTE_BRANCHES, REMOTE_TAGS)
# For git log and git rev-list: nothing but GitHub's refs, never --all.
GITHUB_GLOBS: tuple[str, ...] = tuple(
    f'--glob={namespace}/*' for namespace in GITHUB_NAMESPACES
)
NOT_ON_GITHUB: str = 'commit {sha7} is not on GitHub, not tagging it'
DIFFER_LIMIT: int = 10
DIFFER_NOTE: str = 'local tags differ from GitHub: {names}'


def ref(name: str) -> str:
    """The private ref that holds GitHub's tag ``name``."""
    return f'{REMOTE_TAGS}/{name}'


def _fetch(checkout: pathlib.Path, command: tuple[str, ...]) -> None:
    """Run one fetch command, raising with its stderr on failure."""
    done: subprocess.CompletedProcess[str] = common.run(
        list(command), cwd=checkout
    )
    if done.returncode != 0:
        raise RuntimeError(f'{" ".join(command)}: {done.stderr.strip()}')


def fetch(checkout: pathlib.Path) -> None:
    """Mirror the tags and branches of origin, raising on failure.

    Tags go into REMOTE_TAGS, branches into ``refs/remotes/origin/``.
    Neither fetch writes, moves or prunes ``refs/tags/``.
    """
    _fetch(checkout, FETCH)
    _fetch(checkout, FETCH_BRANCHES)


def on_github(checkout: pathlib.Path, sha: str) -> bool:
    """Whether a commit is reachable from a ref GitHub is known to have.

    Reachable from ``refs/remotes/origin/*`` or the private tag refs. A
    commit that only a local branch, the stash or a local tag reaches is
    not, and tagging it would publish it.
    """
    done: subprocess.CompletedProcess[str] = common.run(
        [
            *('git', 'for-each-ref', '--format=%(refname)'),
            *('--contains', sha),
            *GITHUB_NAMESPACES,
        ],
        cwd=checkout,
    )
    if done.returncode != 0:
        raise RuntimeError(
            f'git for-each-ref --contains {sha}: {done.stderr.strip()}'
        )
    return bool(done.stdout.strip())


def require_on_github(checkout: pathlib.Path, sha: str) -> None:
    """Raise RuntimeError unless the commit is on GitHub, see on_github."""
    if not on_github(checkout, sha):
        raise RuntimeError(NOT_ON_GITHUB.format(sha7=sha[:7]))


def _verify(checkout: pathlib.Path, revision: str) -> str | None:
    """The object a revision names, or None when it does not resolve."""
    done: subprocess.CompletedProcess[str] = common.run(
        ['git', 'rev-parse', '-q', '--verify', revision], cwd=checkout
    )
    if done.returncode != 0:
        return None
    return done.stdout.strip() or None


def commit(checkout: pathlib.Path, name: str) -> str | None:
    """The commit GitHub's tag ``name`` points at, None when not fetched."""
    return _verify(checkout, f'{ref(name)}^{{commit}}')


def local_object(checkout: pathlib.Path, name: str) -> str | None:
    """The object of the local tag ``name``, None when there is none.

    Whatever it points at: only whether it exists is ever decided on.
    """
    return _verify(checkout, f'{LOCAL_TAGS}/{name}')


def record_pushed(checkout: pathlib.Path, name: str) -> None:
    """Point the private ref of ``name`` at the local tag just pushed."""
    pushed: str | None = local_object(checkout, name)
    if pushed is None:
        raise RuntimeError(f'tag {name} vanished from the checkout')
    done: subprocess.CompletedProcess[str] = common.run(
        ['git', 'update-ref', ref(name), pushed], cwd=checkout
    )
    if done.returncode != 0:
        raise RuntimeError(
            f'git update-ref {ref(name)}: {done.stderr.strip()}'
        )


def _objects(checkout: pathlib.Path, namespace: str) -> dict[str, str]:
    """``{tag name: object}`` of every ref under a namespace."""
    done: subprocess.CompletedProcess[str] = common.run(
        [
            *('git', 'for-each-ref'),
            *('--format=%(objectname) %(refname)', namespace),
        ],
        cwd=checkout,
    )
    if done.returncode != 0:
        raise RuntimeError(
            f'git for-each-ref {namespace}: {done.stderr.strip()}'
        )
    prefix: str = f'{namespace}/'
    objects: dict[str, str] = {}
    for line in done.stdout.splitlines():
        objectname, _, refname = line.partition(' ')
        if refname.startswith(prefix):
            objects[refname[len(prefix) :]] = objectname
    return objects


def differing(checkout: pathlib.Path) -> list[str]:
    """Local tags whose object is not the one GitHub has under that name.

    A tag only one side has is not listed. Version tags come first in
    version order, the other names after them.
    """
    local: dict[str, str] = _objects(checkout, LOCAL_TAGS)
    remote: dict[str, str] = _objects(checkout, REMOTE_TAGS)
    return sorted(
        (
            name
            for name, objectname in local.items()
            if name in remote and remote[name] != objectname
        ),
        key=_name_order,
    )


def _name_order(name: str) -> tuple[int, Version, str]:
    """Sort key: version tags in version order, other names after them."""
    try:
        return (0, Version(versions.normalise(name)), name)
    except InvalidVersion:
        return (1, Version('0'), name)


def differ_note(names: list[str]) -> str:
    """The report line naming differing tags, at most DIFFER_LIMIT of them."""
    shown: str = ', '.join(names[:DIFFER_LIMIT])
    hidden: int = len(names) - DIFFER_LIMIT
    if hidden > 0:
        shown = f'{shown} and {hidden} more'
    return DIFFER_NOTE.format(names=shown)
