"""``ppt inventory``: PyPI versions against tags, releases and signatures."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from packaging.version import InvalidVersion, Version

from python_project_tools import github, pypi, versions


@dataclasses.dataclass
class VersionRow:
    """One PyPI version and what exists for it on GitHub.

    ``note`` is ``'untagged'``, ``'empty on pypi'``, ``'rebuilt'`` (set by
    backfill later) or an empty string.
    """

    version: str
    files: int
    yanked: bool
    tag: str | None
    release: bool
    dist_assets: int
    asc_assets: int
    sigstore_assets: int
    sha256sums: bool
    note: str


@dataclasses.dataclass
class PackageInventory:
    """Every PyPI version of a package with its GitHub state."""

    package: str
    repo: str | None
    rows: list[VersionRow]
    version_tags_without_release: list[str]
    odd_tags: list[str]


def _order(version: str) -> tuple[int, Version | None, str]:
    """Sort key: PEP 440 order first, unparsable versions last by string."""
    try:
        return (0, versions.sort_key(version), version)
    except InvalidVersion:
        return (1, None, version)


def _row(
    version: str,
    files: list[pypi.PypiFile],
    tags: list[github.Tag],
    releases: dict[str, github.Release],
) -> VersionRow:
    found: github.Tag | None = versions.match_tag(version, tags)
    tag: str | None = found.name if found else None
    release: github.Release | None = releases.get(tag) if tag else None
    names: set[str] = (
        {asset.name for asset in release.assets} if release else set()
    )
    filenames: list[str] = [file.filename for file in files]
    note: str = ''
    if tag is None:
        note = 'untagged'
    elif not files:
        note = 'empty on pypi'
    return VersionRow(
        version=version,
        files=len(files),
        yanked=bool(files) and all(file.yanked for file in files),
        tag=tag,
        release=release is not None,
        dist_assets=sum(name in names for name in filenames),
        asc_assets=sum(f'{name}.asc' in names for name in filenames),
        sigstore_assets=sum(
            f'{name}.sigstore.json' in names for name in filenames
        ),
        sha256sums={'SHA256SUMS', 'SHA256SUMS.asc'} <= names,
        note=note,
    )


def inventory(package: str, repo: str | None = None) -> PackageInventory:
    """Compare the PyPI versions of a package with its GitHub tags."""
    project: pypi.Project = pypi.project(package)
    repo = repo or project.repo
    tags: list[github.Tag] = []
    releases: dict[str, github.Release] = {}
    if repo:
        tags = github.tags(repo)
        releases = {r.tag: r for r in github.releases(repo)}
    rows: list[VersionRow] = [
        _row(version, project.releases[version], tags, releases)
        for version in sorted(project.releases, key=_order)
    ]
    known: set[str] = {versions.normalise(v) for v in project.releases}
    version_tags: list[str] = sorted(
        tag.name
        for tag in tags
        if versions.is_version_tag(tag.name)
        and versions.normalise(tag.name) not in known
    )
    odd: list[str] = sorted(
        tag.name for tag in tags if not versions.is_version_tag(tag.name)
    )
    return PackageInventory(package, repo, rows, version_tags, odd)


def _yes(value: bool) -> str:
    return 'yes' if value else 'no'


def to_markdown(inv: PackageInventory) -> str:
    """Render one package as a heading, a table and two tag lines."""
    columns: list[str] = [
        *('version', 'files', 'yanked', 'tag', 'release'),
        *('dist', 'asc', 'sigstore', 'sums', 'note'),
    ]
    lines: list[str] = [
        f'## {inv.package} ({inv.repo})',
        '',
        '| ' + ' | '.join(columns) + ' |',
        '|' + '|'.join(' --- ' for _ in columns) + '|',
    ]
    for row in inv.rows:
        cells: list[str] = [
            row.version,
            str(row.files),
            _yes(row.yanked),
            row.tag or '-',
            _yes(row.release),
            str(row.dist_assets),
            str(row.asc_assets),
            str(row.sigstore_assets),
            _yes(row.sha256sums),
            row.note,
        ]
        lines.append('| ' + ' | '.join(cells) + ' |')
    version_tags: str = ', '.join(inv.version_tags_without_release) or 'none'
    odd: str = ', '.join(inv.odd_tags) or 'none'
    lines += [
        '',
        f'Version tags without a PyPI release: {version_tags}',
        f'Other tags: {odd}',
    ]
    return '\n'.join(lines)


def to_json(invs: list[PackageInventory]) -> str:
    """Render inventories as one JSON document."""
    return json.dumps(
        [dataclasses.asdict(inv) for inv in invs], indent=1, sort_keys=True
    )


def main(argv: list[str]) -> int:
    """Run ``ppt inventory``: 0 done, 2 a package had no repository."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog='ppt inventory',
        description='Compare PyPI versions with GitHub tags and releases.',
    )
    parser.add_argument('packages', nargs='+', metavar='PACKAGE')
    parser.add_argument('--repo', metavar='OWNER/NAME')
    parser.add_argument('--json', action='store_true')
    args: argparse.Namespace = parser.parse_args(argv)
    status: int = 0
    invs: list[PackageInventory] = []
    for package in args.packages:
        inv: PackageInventory = inventory(package, args.repo)
        if inv.repo is None:
            print(  # noqa: T201
                f'inventory: {package}: no GitHub repository found, '
                'pass --repo',
                file=sys.stderr,
            )
            status = 2
            continue
        invs.append(inv)
    if args.json:
        print(to_json(invs))  # noqa: T201
    else:
        for inv in invs:
            print(to_markdown(inv))  # noqa: T201
            print()  # noqa: T201
    return status
