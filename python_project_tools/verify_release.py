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
        [--skip pypi,github,stable,docs,install,conda]

Run from the checkout of the package being released.
"""  # noqa: E501

from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable

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


@dataclasses.dataclass
class Result:
    """Outcome of one check."""

    name: str
    status: str  # PASS, FAIL or SKIP
    evidence: str


def run(
    args: list[str], check: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run a command and capture its output as text."""
    return subprocess.run(args, capture_output=True, text=True, check=check)


def fetch_json(url: str) -> dict[str, object] | None:
    """Fetch a JSON document, or return None when the request fails."""
    request: urllib.request.Request = urllib.request.Request(
        url,
        headers={'Cache-Control': 'no-cache', 'User-Agent': 'verify-release'},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None


def poll(seconds: int, probe: Callable[[], Result]) -> Result:
    """Repeat a probe until it passes or the wait runs out."""
    deadline: float = time.monotonic() + seconds
    result: Result = probe()
    while result.status == 'FAIL' and time.monotonic() < deadline:
        time.sleep(15)
        result = probe()
    return result


def check_tag(tag: str) -> Result:
    """The tag exists locally, is annotated, and is on origin."""
    kind: subprocess.CompletedProcess[str] = run(
        ['git', 'cat-file', '-t', tag]
    )
    if kind.returncode != 0:
        return Result('tag', 'FAIL', f'{tag} does not exist locally')
    if kind.stdout.strip() != 'tag':
        return Result(
            'tag', 'FAIL', f'{tag} is a lightweight tag, not annotated'
        )
    remote: subprocess.CompletedProcess[str] = run(
        ['git', 'ls-remote', '--tags', 'origin', tag]
    )
    if tag not in remote.stdout:
        return Result('tag', 'FAIL', f'{tag} is not on origin')
    signed: str = (
        'signed'
        if 'BEGIN PGP' in run(['git', 'cat-file', 'tag', tag]).stdout
        else 'unsigned'
    )
    return Result('tag', 'PASS', f'{tag} annotated, {signed}, on origin')


def check_version(version: str, tag: str) -> Result:
    """pyproject.toml at the tag carries the released version."""
    shown: subprocess.CompletedProcess[str] = run(
        ['git', 'show', f'{tag}:pyproject.toml']
    )
    if shown.returncode != 0:
        return Result('version', 'FAIL', f'no pyproject.toml at {tag}')
    project: dict[str, object] = tomllib.loads(shown.stdout).get('project', {})  # type: ignore[assignment]
    found: object = project.get('version')
    if found != version:
        return Result(
            'version', 'FAIL', f'pyproject.toml at {tag} says {found!r}'
        )
    return Result('version', 'PASS', f'pyproject.toml at {tag} says {version}')


def check_changelog(version: str, tag: str) -> Result:
    """The changelog at the tag has a heading for the version."""
    for name in CHANGELOG_NAMES:
        shown: subprocess.CompletedProcess[str] = run(
            ['git', 'show', f'{tag}:{name}']
        )
        if shown.returncode != 0:
            continue
        pattern: str = rf'^\W*{re.escape(version)}\b'
        if re.search(pattern, shown.stdout, flags=re.MULTILINE):
            return Result(
                'changelog',
                'PASS',
                f'{name} at {tag} has an entry for {version}',
            )
        return Result(
            'changelog',
            'FAIL',
            f'{name} at {tag} has no heading for {version}',
        )
    return Result('changelog', 'FAIL', 'no changelog file found at the tag')


def check_pypi(package: str, version: str) -> Result:
    """PyPI lists the version with both an sdist and a wheel."""
    data: dict[str, object] | None = fetch_json(
        f'https://pypi.org/pypi/{package}/{version}/json'
    )
    if data is None:
        return Result(
            'pypi',
            'FAIL',
            f'https://pypi.org/project/{package}/{version}/ not found',
        )
    files: list[dict[str, str]] = data.get('urls', [])  # ty: ignore[invalid-assignment]
    kinds: set[str] = {entry['packagetype'] for entry in files}
    missing: set[str] = {'sdist', 'bdist_wheel'} - kinds
    if missing:
        return Result(
            'pypi',
            'FAIL',
            f'{version} on PyPI without {", ".join(sorted(missing))}',
        )
    return Result(
        'pypi',
        'PASS',
        f'https://pypi.org/project/{package}/{version}/ has sdist and wheel',
    )


def check_github_release(tag: str) -> Result:
    """A GitHub release exists for the tag and is the latest one."""
    listed: subprocess.CompletedProcess[str] = run(
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
        return Result(
            'github-release',
            'FAIL',
            f'gh release list: {listed.stderr.strip()}',
        )
    releases: list[dict[str, object]] = json.loads(listed.stdout)
    matches: list[dict[str, object]] = [
        item for item in releases if item['tagName'] == tag
    ]
    if not matches:
        return Result('github-release', 'FAIL', f'no GitHub release for {tag}')
    release: dict[str, object] = matches[0]
    url: str = run(
        ['gh', 'release', 'view', tag, '--json', 'url', '--jq', '.url']
    ).stdout.strip()
    if release.get('isDraft'):
        return Result('github-release', 'FAIL', f'{url} is still a draft')
    latest: str = 'latest' if release.get('isLatest') else 'not marked latest'
    return Result('github-release', 'PASS', f'{url} ({latest})')


def check_stable_branch(tag: str, branch: str) -> Result:
    """The stable branch on origin contains the tag."""
    fetched: subprocess.CompletedProcess[str] = run(
        ['git', 'fetch', 'origin', branch]
    )
    if fetched.returncode != 0:
        return Result(
            'stable-branch',
            'FAIL',
            f'git fetch origin {branch}: {fetched.stderr.strip()}',
        )
    count: subprocess.CompletedProcess[str] = run(
        ['git', 'rev-list', '--count', f'origin/{branch}..{tag}']
    )
    if count.returncode != 0:
        return Result('stable-branch', 'FAIL', count.stderr.strip())
    behind: int = int(count.stdout.strip())
    if behind:
        return Result(
            'stable-branch',
            'FAIL',
            f'origin/{branch} is {behind} commits behind {tag}',
        )
    return Result('stable-branch', 'PASS', f'origin/{branch} contains {tag}')


def check_docs(slug: str, tag: str) -> Result:
    """Read the Docs finished a successful build at or after the tag commit."""
    tag_commit: str = run(['git', 'rev-list', '-n', '1', tag]).stdout.strip()
    url: str = f'https://readthedocs.org/api/v3/projects/{slug}/versions/latest/builds/?limit=1'
    data: dict[str, object] | None = fetch_json(url)
    if data is None or not data.get('results'):
        return Result('docs', 'FAIL', f'no builds at {url}')
    build: dict[str, object] = data['results'][0]  # ty: ignore[not-subscriptable]
    state: str = build['state']['code']  # ty: ignore[not-subscriptable]
    commit: str = str(build.get('commit'))
    if commit != tag_commit:
        ahead: subprocess.CompletedProcess[str] = run(
            ['git', 'merge-base', '--is-ancestor', tag_commit, commit]
        )
        if ahead.returncode != 0:
            return Result(
                'docs',
                'FAIL',
                f'latest build {build["id"]} is for {commit[:10]}, before {tag}',  # noqa: E501
            )
    if state != 'finished':
        return Result('docs', 'FAIL', f'build {build["id"]} is {state}')
    if not build.get('success'):
        return Result('docs', 'FAIL', f'build {build["id"]} failed')
    return Result(
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


def check_conda(package: str, version: str, conda_name: str | None) -> Result:
    """conda-forge lists the version, or there is no conda-forge package at all."""  # noqa: E501
    for name in conda_candidates(package, conda_name):
        data: dict[str, object] | None = fetch_json(
            f'https://api.anaconda.org/package/conda-forge/{name}'
        )
        if data is None:
            continue
        versions: list[str] = [str(item) for item in data.get('versions', [])]  # ty: ignore[not-iterable]
        if version in versions:
            return Result(
                'conda-forge',
                'PASS',
                f'https://anaconda.org/conda-forge/{name} lists {version}',
            )
        latest: str = versions[-1] if versions else 'nothing'
        return Result(
            'conda-forge',
            'FAIL',
            f'conda-forge/{name} latest is {latest}, no {version}',
        )
    return Result(
        'conda-forge',
        'SKIP',
        f'no conda-forge package named {" or ".join(conda_candidates(package, conda_name))}',  # noqa: E501
    )


def check_install(package: str, version: str, import_name: str) -> Result:
    """A fresh virtualenv installs the release from PyPI and imports it."""
    if shutil.which('uv') is None:
        return Result('install', 'SKIP', 'uv is not installed')
    with tempfile.TemporaryDirectory() as workdir:
        venv: pathlib.Path = pathlib.Path(workdir) / 'venv'
        # Run from the temporary directory so the checkout's own uv settings,
        # such as an exclude-newer cutoff, do not filter the release away.
        run(['uv', 'venv', '--quiet', str(venv)], check=True)
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
            return Result(
                'install', 'FAIL', installed.stderr.strip().splitlines()[-1]
            )
        python: pathlib.Path = venv / 'bin' / 'python'
        code: str = (
            f'import importlib.metadata as m, {import_name}; '
            f'print(m.version({package!r}))'
        )
        imported: subprocess.CompletedProcess[str] = run(
            [str(python), '-I', '-c', code]
        )
    if imported.returncode != 0:
        return Result(
            'install', 'FAIL', imported.stderr.strip().splitlines()[-1]
        )
    reported: str = imported.stdout.strip()
    if reported != version:
        return Result(
            'install', 'FAIL', f'installed metadata reports {reported}'
        )
    return Result(
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
        help='comma separated: pypi,github,stable,docs,install,conda',
    )
    return parser.parse_args(argv)


def read_package_name(root: pathlib.Path = pathlib.Path('.')) -> str:
    """Return the name from ``root/pyproject.toml``, or '' when absent."""
    pyproject: pathlib.Path = root / 'pyproject.toml'
    project: dict[str, object] = {}
    if pyproject.exists():
        project = tomllib.loads(pyproject.read_text()).get('project', {})  # type: ignore[assignment]
    return str(project.get('name', ''))


def main(argv: list[str]) -> int:
    """Run every check and print the table."""
    args: argparse.Namespace = parse_args(argv)
    package: str = args.package or read_package_name()
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

    results: list[Result] = [
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

    width: int = max(len(result.name) for result in results)
    for result in results:
        print(f'{result.status:4} {result.name:{width}}  {result.evidence}')  # noqa: T201
    failed: int = sum(result.status == 'FAIL' for result in results)
    print(f'\n{len(results) - failed}/{len(results)} checks passed')  # noqa: T201
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
