"""Verify that a release of a Python package is complete.

A release has five parts: the version bump with its changelog entry, the
pushed tag, the package on PyPI, the GitHub release, and the stable branch
fast-forwarded to the tag, plus the conda-forge upload when the package has
a feedstock. CI usually covers only the PyPI part, so the others are checked
here one by one. Every check prints a line with its
evidence and the exit status is 1 when any check fails.

Usage:
    ppt verify-release --version 4.1.0 [--package python-utils]
        [--tag v4.1.0] [--stable-branch master] [--rtd-slug python-utils]
        [--wait 900] [--import-name python_utils] [--conda-name python-utils]
        [--skip pypi,github,stable,docs,install,conda,signatures]

Run from the checkout of the package being released.
"""  # noqa: E501

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable

from python_project_tools import common, github
from python_project_tools.toml_compat import tomllib

CHANGELOG_NAMES: tuple[str, ...] = (
    'CHANGELOG.md',
    'CHANGELOG.rst',
    'CHANGELOG',
    'CHANGES.md',
    'CHANGES.rst',
    'HISTORY.md',
    'HISTORY.rst',
)


def poll(seconds: int, probe: Callable[[], common.Result]) -> common.Result:
    """Repeat a probe until it passes or the wait runs out."""
    deadline: float = time.monotonic() + seconds
    result: common.Result = probe()
    while result.status == 'FAIL' and time.monotonic() < deadline:
        time.sleep(15)
        result = probe()
    return result


def check_tag(tag: str) -> common.Result:
    """The tag exists locally, is annotated, and is on origin."""
    kind: subprocess.CompletedProcess[str] = common.run(
        ['git', 'cat-file', '-t', tag]
    )
    if kind.returncode != 0:
        return common.Result('tag', 'FAIL', f'{tag} does not exist locally')
    if kind.stdout.strip() != 'tag':
        return common.Result(
            'tag', 'FAIL', f'{tag} is a lightweight tag, not annotated'
        )
    remote: subprocess.CompletedProcess[str] = common.run(
        ['git', 'ls-remote', '--tags', 'origin', tag]
    )
    if tag not in remote.stdout:
        return common.Result('tag', 'FAIL', f'{tag} is not on origin')
    signed: str = (
        'signed'
        if 'BEGIN PGP' in common.run(['git', 'cat-file', 'tag', tag]).stdout
        else 'unsigned'
    )
    return common.Result(
        'tag', 'PASS', f'{tag} annotated, {signed}, on origin'
    )


def check_version(version: str, tag: str) -> common.Result:
    """pyproject.toml at the tag carries the released version."""
    shown: subprocess.CompletedProcess[str] = common.run(
        ['git', 'show', f'{tag}:pyproject.toml']
    )
    if shown.returncode != 0:
        return common.Result('version', 'FAIL', f'no pyproject.toml at {tag}')
    project: dict[str, object] = tomllib.loads(shown.stdout).get('project', {})  # type: ignore[assignment]
    found: object = project.get('version')
    if found != version:
        return common.Result(
            'version', 'FAIL', f'pyproject.toml at {tag} says {found!r}'
        )
    return common.Result(
        'version', 'PASS', f'pyproject.toml at {tag} says {version}'
    )


def check_changelog(version: str, tag: str) -> common.Result:
    """The changelog at the tag has a heading for the version."""
    for name in CHANGELOG_NAMES:
        shown: subprocess.CompletedProcess[str] = common.run(
            ['git', 'show', f'{tag}:{name}']
        )
        if shown.returncode != 0:
            continue
        pattern: str = rf'^\W*{re.escape(version)}\b'
        if re.search(pattern, shown.stdout, flags=re.MULTILINE):
            return common.Result(
                'changelog',
                'PASS',
                f'{name} at {tag} has an entry for {version}',
            )
        return common.Result(
            'changelog',
            'FAIL',
            f'{name} at {tag} has no heading for {version}',
        )
    return common.Result(
        'changelog', 'FAIL', 'no changelog file found at the tag'
    )


def check_pypi(package: str, version: str) -> common.Result:
    """PyPI lists the version with both an sdist and a wheel."""
    data: object | None
    try:
        data = common.fetch_json(
            f'https://pypi.org/pypi/{package}/{version}/json'
        )
    except common.FetchError as err:
        return common.Result(name='pypi', status='FAIL', evidence=str(err))
    if not isinstance(data, dict):
        return common.Result(
            'pypi',
            'FAIL',
            f'https://pypi.org/project/{package}/{version}/ not found',
        )
    files: list[dict[str, str]] = data.get('urls', [])
    kinds: set[str] = {entry['packagetype'] for entry in files}
    missing: set[str] = {'sdist', 'bdist_wheel'} - kinds
    if missing:
        return common.Result(
            'pypi',
            'FAIL',
            f'{version} on PyPI without {", ".join(sorted(missing))}',
        )
    return common.Result(
        'pypi',
        'PASS',
        f'https://pypi.org/project/{package}/{version}/ has sdist and wheel',
    )


def check_github_release(tag: str) -> common.Result:
    """A GitHub release exists for the tag and is the latest one."""
    listed: subprocess.CompletedProcess[str] = common.run(
        [
            'gh',
            'release',
            'list',
            '--limit',
            '30',
            '--json',
            'tagName,isLatest,isDraft',
        ]
    )
    if listed.returncode != 0:
        return common.Result(
            'github-release',
            'FAIL',
            f'gh release list: {listed.stderr.strip()}',
        )
    releases: list[dict[str, object]] = json.loads(listed.stdout)
    matches: list[dict[str, object]] = [
        item for item in releases if item['tagName'] == tag
    ]
    if not matches:
        return common.Result(
            'github-release', 'FAIL', f'no GitHub release for {tag}'
        )
    release: dict[str, object] = matches[0]
    url: str = common.run(
        ['gh', 'release', 'view', tag, '--json', 'url', '--jq', '.url']
    ).stdout.strip()
    if release.get('isDraft'):
        return common.Result(
            'github-release', 'FAIL', f'{url} is still a draft'
        )
    latest: str = 'latest' if release.get('isLatest') else 'not marked latest'
    return common.Result('github-release', 'PASS', f'{url} ({latest})')


def check_signatures(tag: str) -> common.Result:
    """The GitHub release carries GPG and Sigstore signatures for each dist."""
    viewed: subprocess.CompletedProcess[str] = common.run(
        [
            'gh',
            'repo',
            'view',
            '--json',
            'nameWithOwner',
            '--jq',
            '.nameWithOwner',
        ]
    )
    repo: str = viewed.stdout.strip()
    if viewed.returncode != 0 or not repo:
        return common.Result(
            'signatures',
            'FAIL',
            f'gh repo view: {viewed.stderr.strip()}',
        )
    try:
        listed: list[github.Release] = github.releases(repo)
    except RuntimeError as error:
        return common.Result('signatures', 'FAIL', str(error))
    matches: list[github.Release] = [
        item for item in listed if item.tag == tag
    ]
    if not matches:
        return common.Result('signatures', 'FAIL', f'no release for {tag}')
    names: set[str] = {asset.name for asset in matches[0].assets}
    dists: list[str] = sorted(
        name for name in names if name.endswith(('.whl', '.tar.gz'))
    )
    wanted: list[str] = ['SHA256SUMS', 'SHA256SUMS.asc']
    for dist in dists:
        wanted += [f'{dist}.asc', f'{dist}.sigstore.json']
    missing: list[str] = [name for name in wanted if name not in names]
    if missing:
        return common.Result(
            'signatures', 'FAIL', f'missing {", ".join(missing)}'
        )
    return common.Result(
        'signatures',
        'PASS',
        f'{len(dists)} files with .asc and .sigstore.json, SHA256SUMS signed',
    )


def check_stable_branch(tag: str, branch: str) -> common.Result:
    """The stable branch on origin contains the tag."""
    fetched: subprocess.CompletedProcess[str] = common.run(
        ['git', 'fetch', 'origin', branch]
    )
    if fetched.returncode != 0:
        return common.Result(
            'stable-branch',
            'FAIL',
            f'git fetch origin {branch}: {fetched.stderr.strip()}',
        )
    count: subprocess.CompletedProcess[str] = common.run(
        ['git', 'rev-list', '--count', f'origin/{branch}..{tag}']
    )
    if count.returncode != 0:
        return common.Result('stable-branch', 'FAIL', count.stderr.strip())
    behind: int = int(count.stdout.strip())
    if behind:
        return common.Result(
            'stable-branch',
            'FAIL',
            f'origin/{branch} is {behind} commits behind {tag}',
        )
    return common.Result(
        'stable-branch', 'PASS', f'origin/{branch} contains {tag}'
    )


def check_docs(slug: str, tag: str) -> common.Result:
    """Read the Docs finished a successful build at or after the tag commit."""
    tag_commit: str = common.run(
        ['git', 'rev-list', '-n', '1', tag]
    ).stdout.strip()
    url: str = f'https://readthedocs.org/api/v3/projects/{slug}/versions/latest/builds/?limit=1'
    data: object | None
    try:
        data = common.fetch_json(url)
    except common.FetchError as err:
        return common.Result(name='docs', status='FAIL', evidence=str(err))
    if not isinstance(data, dict) or not data.get('results'):
        return common.Result('docs', 'FAIL', f'no builds at {url}')
    build: dict[str, object] = data['results'][0]
    state: str = build['state']['code']  # ty: ignore[not-subscriptable]
    commit: str = str(build.get('commit'))
    if commit != tag_commit:
        ahead: subprocess.CompletedProcess[str] = common.run(
            ['git', 'merge-base', '--is-ancestor', tag_commit, commit]
        )
        if ahead.returncode != 0:
            return common.Result(
                'docs',
                'FAIL',
                f'latest build {build["id"]} is for {commit[:10]}, before {tag}',  # noqa: E501
            )
    if state != 'finished':
        return common.Result('docs', 'FAIL', f'build {build["id"]} is {state}')
    if not build.get('success'):
        return common.Result('docs', 'FAIL', f'build {build["id"]} failed')
    return common.Result(
        'docs',
        'PASS',
        f'https://readthedocs.org/projects/{slug}/builds/{build["id"]}/ succeeded',  # noqa: E501
    )


def conda_candidates(package: str, conda_name: str | None) -> list[str]:
    """Names anaconda.org might know the package by, most likely first."""
    if conda_name:
        return [conda_name]
    lowered: str = package.lower()
    names: list[str] = [
        lowered,
        lowered.replace('_', '-'),
        re.sub(r'[-_.]', '', lowered),
    ]
    return list(dict.fromkeys(names))


def check_conda(
    package: str, version: str, conda_name: str | None
) -> common.Result:
    """conda-forge lists the version, or there is no conda-forge package at all."""  # noqa: E501
    for name in conda_candidates(package, conda_name):
        data: object | None
        try:
            data = common.fetch_json(
                f'https://api.anaconda.org/package/conda-forge/{name}'
            )
        except common.FetchError as err:
            return common.Result(
                name='conda-forge', status='FAIL', evidence=str(err)
            )
        if not isinstance(data, dict):
            continue
        versions: list[str] = [str(item) for item in data.get('versions', [])]
        if version in versions:
            return common.Result(
                'conda-forge',
                'PASS',
                f'https://anaconda.org/conda-forge/{name} lists {version}',
            )
        latest: str = versions[-1] if versions else 'nothing'
        return common.Result(
            'conda-forge',
            'FAIL',
            f'conda-forge/{name} latest is {latest}, no {version}',
        )
    return common.Result(
        'conda-forge',
        'SKIP',
        f'no conda-forge package named {" or ".join(conda_candidates(package, conda_name))}',  # noqa: E501
    )


def check_install(
    package: str, version: str, import_name: str
) -> common.Result:
    """A fresh virtualenv installs the release from PyPI and imports it."""
    if shutil.which('uv') is None:
        return common.Result('install', 'SKIP', 'uv is not installed')
    with tempfile.TemporaryDirectory() as workdir:
        venv: pathlib.Path = pathlib.Path(workdir) / 'venv'
        # Run from the temporary directory so the checkout's own uv settings,
        # such as an exclude-newer cutoff, do not filter the release away.
        common.run(['uv', 'venv', '--quiet', str(venv)], check=True)
        installed: subprocess.CompletedProcess[str] = subprocess.run(
            [
                'uv',
                'pip',
                'install',
                '--quiet',
                '--no-cache',
                '--no-config',
                '--python',
                str(venv),
                f'{package}=={version}',
            ],
            capture_output=True,
            text=True,
            cwd=workdir,
        )
        if installed.returncode != 0:
            return common.Result(
                'install', 'FAIL', installed.stderr.strip().splitlines()[-1]
            )
        python: pathlib.Path = venv / 'bin' / 'python'
        code: str = (
            f'import importlib.metadata as m, {import_name}; '
            f'print(m.version({package!r}))'
        )
        imported: subprocess.CompletedProcess[str] = common.run(
            [str(python), '-I', '-c', code]
        )
    if imported.returncode != 0:
        return common.Result(
            'install', 'FAIL', imported.stderr.strip().splitlines()[-1]
        )
    reported: str = imported.stdout.strip()
    if reported != version:
        return common.Result(
            'install', 'FAIL', f'installed metadata reports {reported}'
        )
    return common.Result(
        'install',
        'PASS',
        f'{package}=={version} installs and imports {import_name}',
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the command line."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog='ppt verify-release', description=(__doc__ or '').split('\n\n')[0]
    )
    parser.add_argument(
        '--version', required=True, help='released version, e.g. 4.1.0'
    )
    parser.add_argument(
        '--package', help='PyPI name, read from pyproject.toml by default'
    )
    parser.add_argument(
        '--import-name',
        help='module to import after install, default: package with - as _',
    )
    parser.add_argument('--tag', help='git tag, default: v<version>')
    parser.add_argument('--stable-branch', default='master')
    parser.add_argument(
        '--rtd-slug', help='Read the Docs project slug, default: package name'
    )
    parser.add_argument(
        '--wait',
        type=int,
        default=0,
        help='seconds to keep polling PyPI and docs',
    )
    parser.add_argument(
        '--conda-name',
        help='conda-forge package name when it differs from PyPI',
    )
    parser.add_argument(
        '--skip',
        default='',
        help=(
            'comma separated: pypi,github,stable,docs,install,conda,signatures'
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """Run every check and print the table."""
    args: argparse.Namespace = parse_args(argv)
    package: str = args.package or common.read_package_name()
    if not package:
        print(  # noqa: T201
            'no package name: pass --package or run inside a checkout',
            file=sys.stderr,
        )
        return 2
    import_name: str = args.import_name or package.replace('-', '_')
    tag: str = args.tag or f'v{args.version}'
    slug: str = args.rtd_slug or package
    skip: set[str] = {item for item in args.skip.split(',') if item}

    results: list[common.Result] = [
        check_tag(tag),
        check_version(args.version, tag),
        check_changelog(args.version, tag),
    ]
    if 'pypi' not in skip:
        results.append(
            poll(args.wait, lambda: check_pypi(package, args.version))
        )
    if 'github' not in skip:
        results.append(check_github_release(tag))
    if 'stable' not in skip:
        results.append(check_stable_branch(tag, args.stable_branch))
    if 'docs' not in skip:
        results.append(poll(args.wait, lambda: check_docs(slug, tag)))
    if 'install' not in skip and 'pypi' not in skip:
        results.append(check_install(package, args.version, import_name))
    if 'conda' not in skip:
        results.append(check_conda(package, args.version, args.conda_name))
    if 'signatures' not in skip:
        results.append(check_signatures(tag))

    width: int = max(len(result.name) for result in results)
    for result in results:
        print(f'{result.status:4} {result.name:{width}}  {result.evidence}')  # noqa: T201
    failed: int = sum(result.status == 'FAIL' for result in results)
    print(f'\n{len(results) - failed}/{len(results)} checks passed')  # noqa: T201
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
