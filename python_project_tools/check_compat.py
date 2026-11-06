"""Check the working tree against the last release for anything that breaks users.

Four checks, each against the released artefacts rather than the git history:

* api: the public surface of the old and the new wheel, imported side by side
  on every Python asked for, compared name by name and parameter by parameter.
* tests: the test suite of the old release, run against the new code. A test
  that passes on the old release and fails on the new one is a break.
* metadata: Requires-Python, dependency ranges, extras and entry points of the
  two wheels.
* griffe: ``griffe check`` against the release tag, a static second opinion
  whose extra findings are reported as warnings.

Run from the checkout on Python 3.11 or later, with packaging available. The
interpreters under test can be older, down to 3.9::

    uv run --no-project --python '>=3.11' --with packaging python -I check_compat.py --against 4.1.0
        [--from auto|pypi|tag] [--tag v4.1.0] [--python 3.10 --python 3.14]
        [--skip api,tests,metadata,griffe] [--json findings.json] [--keep]

Exit status 1 means at least one break, 2 means the check could not run.
"""  # noqa: D415, E501

from __future__ import annotations

import argparse
import configparser
import dataclasses
import email.parser
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile

import tomllib  # ty: ignore[import-not-found]

from python_project_tools import api_manifest, compat_rules
from python_project_tools.compat_rules import Finding

PYTEST_CONFIGS: tuple[str, ...] = (
    'pytest.ini',
    'pyproject.toml',
    'setup.cfg',
    'tox.ini',
)


class SetupError(Exception):
    """The check could not be set up, so no verdict is possible."""


@dataclasses.dataclass
class Release:
    """The old release: its wheel, its source tree, and where both came from."""  # noqa: E501

    wheel: pathlib.Path
    source: pathlib.Path
    origin: str


def run(
    cmd: list[str],
    cwd: pathlib.Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 900,
) -> subprocess.CompletedProcess[str]:
    """Run a command and capture its output as text."""
    return subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout
    )


def must(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Run a command and raise SetupError when it fails."""
    done: subprocess.CompletedProcess[str] = run(cmd, **kwargs)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    if done.returncode != 0:
        tail: str = ' | '.join(
            (done.stderr or done.stdout).strip().splitlines()[-5:]
        )
        raise SetupError(f'{" ".join(cmd[:4])} failed: {tail}')
    return done


def fetch(url: str, dest: pathlib.Path | None = None) -> bytes | None:
    """Download a URL, to a file when dest is given. None on HTTP 404."""
    request: urllib.request.Request = urllib.request.Request(
        url, headers={'User-Agent': 'check-compat'}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data: bytes = response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    if dest is not None:
        dest.write_bytes(data)
    return data


def old_from_pypi(
    name: str, version: str, work: pathlib.Path
) -> Release | None:
    """The released wheel and sdist from PyPI, or None when PyPI lacks either."""  # noqa: E501
    raw: bytes | None = fetch(f'https://pypi.org/pypi/{name}/{version}/json')
    if raw is None:
        return None
    files: list[dict[str, str]] = json.loads(raw)['urls']
    wheels: list[dict[str, str]] = [
        f for f in files if f['filename'].endswith('-none-any.whl')
    ]
    sdists: list[dict[str, str]] = [
        f for f in files if f['packagetype'] == 'sdist'
    ]
    if not wheels or not sdists:
        return None
    dist: pathlib.Path = work / 'old-dist'
    dist.mkdir()
    wheel: pathlib.Path = dist / wheels[0]['filename']
    fetch(wheels[0]['url'], wheel)
    archive: pathlib.Path = dist / sdists[0]['filename']
    fetch(sdists[0]['url'], archive)
    unpacked: pathlib.Path = work / 'old-sdist'
    with tarfile.open(archive) as tar:
        tar.extractall(unpacked, filter='data')
    roots: list[pathlib.Path] = [p for p in unpacked.iterdir() if p.is_dir()]
    return Release(
        wheel,
        roots[0] if len(roots) == 1 else unpacked,
        f'PyPI {name}=={version}',
    )


def old_from_tag(
    tag: str, checkout: pathlib.Path, work: pathlib.Path
) -> Release:
    """Build the old wheel from the release tag, and use the tag's tree as its source."""  # noqa: E501
    source: pathlib.Path = work / 'old-tree'
    source.mkdir()
    archive: subprocess.CompletedProcess[bytes] = subprocess.run(
        ['git', 'archive', '--format=tar', tag],
        cwd=checkout,
        capture_output=True,
    )
    if archive.returncode != 0:
        raise SetupError(
            f'git archive {tag} failed: {archive.stderr.decode().strip()}'
        )
    subprocess.run(
        ['tar', '-x', '-C', str(source)], input=archive.stdout, check=True
    )
    dist: pathlib.Path = work / 'old-dist'
    must(['uv', 'build', '--wheel', '--out-dir', str(dist), str(source)])
    return Release(
        next(dist.glob('*.whl')), source, f'tag {tag}, built locally'
    )


def build_new_wheel(
    checkout: pathlib.Path, work: pathlib.Path
) -> pathlib.Path:
    """Build a wheel from the working tree, uncommitted changes included."""
    dist: pathlib.Path = work / 'new-dist'
    must(['uv', 'build', '--wheel', '--out-dir', str(dist)], cwd=checkout)
    return next(dist.glob('*.whl'))


def top_level_names(wheel: pathlib.Path) -> list[str]:
    """Public top-level packages and modules a wheel installs."""
    names: set[str] = set()
    with zipfile.ZipFile(wheel) as archive:
        for entry in archive.namelist():
            first: str = entry.split('/')[0]
            if first.endswith(('.dist-info', '.data')) or first.startswith(
                '_'
            ):
                continue
            if '/' in entry and entry.endswith('.py'):
                names.add(first)
            elif entry.endswith('.py'):
                names.add(first[:-3])
    return sorted(names)


def wheel_metadata(wheel: pathlib.Path) -> dict[str, object]:
    """Requires-Python, Requires-Dist, extras and entry points of a wheel."""
    with zipfile.ZipFile(wheel) as archive:
        info: str = next(
            n for n in archive.namelist() if n.endswith('.dist-info/METADATA')
        )
        message = email.parser.Parser().parsestr(archive.read(info).decode())
        points_name: str = info.replace('METADATA', 'entry_points.txt')
        points_text: str = (
            archive.read(points_name).decode()
            if points_name in archive.namelist()
            else ''
        )
    parser: configparser.ConfigParser = configparser.ConfigParser(
        delimiters=('=',), interpolation=None
    )
    parser.optionxform = str  # type: ignore[assignment,method-assign]  # ty: ignore[invalid-assignment]
    parser.read_string(points_text)
    return {
        'requires_python': message.get('Requires-Python'),
        'requires_dist': message.get_all('Requires-Dist') or [],
        'extras': message.get_all('Provides-Extra') or [],
        'entry_points': {
            group: dict(parser[group]) for group in parser.sections()
        },
    }


def sync_env(
    checkout: pathlib.Path, python: str | None, work: pathlib.Path
) -> pathlib.Path:
    """A private venv with the project's locked dependencies but not the project itself."""  # noqa: E501
    venv: pathlib.Path = work / f'venv-{python or "default"}'
    env: dict[str, str] = {**os.environ, 'UV_PROJECT_ENVIRONMENT': str(venv)}
    pin: list[str] = ['--python', python] if python else []
    if (checkout / 'uv.lock').exists():
        must(
            [
                'uv',
                'sync',
                '--frozen',
                '--all-groups',
                '--all-extras',
                '--no-install-project',
                '--quiet',
                *pin,
            ],
            cwd=checkout,
            env=env,
        )
    else:
        must(['uv', 'venv', '--quiet', *pin, str(venv)], cwd=work)
    return venv / 'bin' / 'python'


def install(
    python: pathlib.Path, wheel: pathlib.Path, target: pathlib.Path
) -> None:
    """Install one wheel, without dependencies, into its own directory."""
    must(
        [
            'uv',
            'pip',
            'install',
            '--quiet',
            '--no-deps',
            '--no-config',
            '--python',
            str(python),
            '--target',
            str(target),
            str(wheel),
        ],
        cwd=target.parent,
    )


def ensure_deps(
    python: pathlib.Path, wheel: pathlib.Path, checkout: pathlib.Path
) -> None:
    """Without a lock file, install the wheel's own dependencies and pytest into the venv."""  # noqa: E501
    if (checkout / 'uv.lock').exists():
        return
    must(
        [
            'uv',
            'pip',
            'install',
            '--quiet',
            '--python',
            str(python),
            str(wheel),
            'pytest',
        ],
        cwd=wheel.parent,
    )
    must(
        [
            'uv',
            'pip',
            'uninstall',
            '--quiet',
            '--python',
            str(python),
            wheel.name.split('-')[0],
        ],
        cwd=wheel.parent,
    )


def manifest(
    python: pathlib.Path, target: pathlib.Path, names: list[str]
) -> dict[str, object]:
    """The API manifest of the packages installed in target."""
    packages: list[str] = [
        arg for name in names for arg in ('--package', name)
    ]
    done: subprocess.CompletedProcess[str] = must(
        [
            str(python),
            '-I',
            str(pathlib.Path(api_manifest.__file__)),
            '--path',
            str(target),
            *packages,
        ],
        cwd=target,
    )
    return json.loads(done.stdout)


def prepare_old_tests(
    release: Release, names: list[str], work: pathlib.Path
) -> pathlib.Path | None:
    """A copy of the old source tree with the package removed, so its tests import the code under test."""  # noqa: E501
    tests: pathlib.Path = work / 'old-tests'
    shutil.copytree(release.source, tests, symlinks=True)
    for name in names:
        for candidate in (
            tests / name,
            tests / 'src' / name,
            tests / f'{name}.py',
            tests / 'src' / f'{name}.py',
        ):
            if candidate.is_dir():
                shutil.rmtree(candidate)
            elif candidate.exists():
                candidate.unlink()
    found: bool = any(
        path.name.startswith('test') or path.name.endswith('_test.py')
        for path in tests.rglob('*.py')
    )
    return tests if found else None


def pytest_args(python: pathlib.Path, tests: pathlib.Path) -> list[str]:
    """Arguments that run the old suite as configured, minus coverage gates and caching."""  # noqa: E501
    args: list[str] = [
        '-m',
        'pytest',
        '-q',
        '-rfE',
        '--no-header',
        '-p',
        'no:cacheprovider',
    ]
    configs: str = ''.join(
        (tests / name).read_text(errors='replace')
        for name in PYTEST_CONFIGS
        if (tests / name).exists()
    )
    has_cov: bool = (
        run([str(python), '-c', 'import pytest_cov']).returncode == 0
    )
    if '--cov' in configs and has_cov:
        args.append('--no-cov')
    return args


def run_suite(
    python: pathlib.Path,
    tests: pathlib.Path,
    target: pathlib.Path,
    nodes: list[str] | None = None,
) -> tuple[int, set[str], str]:
    """Run the old suite, or some of its tests, against the package in target.

    Returns the exit status, the failing node ids and the tail of the output.
    """
    env: dict[str, str] = {
        **os.environ,
        'PYTHONPATH': str(target),
        'PYTHONHASHSEED': '0',
    }
    env.pop('PYTEST_ADDOPTS', None)
    cmd: list[str] = [str(python), *pytest_args(python, tests), *(nodes or [])]
    done: subprocess.CompletedProcess[str] = run(cmd, cwd=tests, env=env)
    failed: set[str] = set()
    for line in done.stdout.splitlines():
        if line.startswith(('FAILED ', 'ERROR ')):
            failed.add(line.split(' ', 1)[1].split(' - ')[0].strip())
    tail: str = ' | '.join(done.stdout.strip().splitlines()[-3:])
    return done.returncode, failed, tail


def test_findings(
    python: pathlib.Path,
    tests: pathlib.Path,
    old: pathlib.Path,
    new: pathlib.Path,
    label: str,
) -> list[Finding]:
    """Breaks found by the old release's own test suite."""
    new_rc, new_failed, new_tail = run_suite(python, tests, new)
    if new_rc == 0:
        return []
    if new_rc == 5:
        return [Finding('skip', label, 'old test suite', 'no tests collected')]
    old_rc, old_failed, old_tail = run_suite(python, tests, old)
    if new_rc in (2, 3, 4) and old_rc in (0, 1):
        return [
            Finding(
                'break',
                label,
                'old test suite',
                f'no longer runs against the new code: {new_tail}',
            )
        ]
    if new_rc in (2, 3, 4) or old_rc in (2, 3, 4):
        return [
            Finding(
                'skip', label, 'old test suite', f'cannot run here: {old_tail}'
            )
        ]
    out: list[Finding] = confirm(
        python, tests, old, new, sorted(new_failed - old_failed), label
    )
    for node in sorted(new_failed & old_failed):
        out.append(  # noqa: PERF401
            Finding('warn', label, node, 'fails on the old release as well')
        )
    return out


def confirm(
    python: pathlib.Path,
    tests: pathlib.Path,
    old: pathlib.Path,
    new: pathlib.Path,
    nodes: list[str],
    label: str,
) -> list[Finding]:
    """Rerun candidate breaks twice on each side, so a flaky test is not reported as a break."""  # noqa: E501
    if not nodes:
        return []
    new_fails: dict[str, int] = dict.fromkeys(nodes, 1)
    old_fails: dict[str, int] = dict.fromkeys(nodes, 0)
    for _ in range(2):
        for target, fails in ((new, new_fails), (old, old_fails)):
            _, failed, _ = run_suite(python, tests, target, nodes)
            for node in nodes:
                fails[node] += node in failed
    out: list[Finding] = []
    for node in nodes:
        if new_fails[node] == 3 and old_fails[node] == 0:
            out.append(
                Finding(
                    'break',
                    label,
                    node,
                    'passes on the old release, fails on the new code (3 of 3 runs)',  # noqa: E501
                )
            )
        else:
            out.append(
                Finding(
                    'warn',
                    label,
                    node,
                    f'flaky: failed {new_fails[node]} of 3 runs on the new code, '  # noqa: E501
                    f'{old_fails[node]} of 3 on the old release',
                )
            )
    return out


def per_python(
    checkout: pathlib.Path,
    python: str | None,
    release: Release,
    new_wheel: pathlib.Path,
    names: list[str],
    skip: set[str],
    work: pathlib.Path,
    tests: pathlib.Path | None,
) -> list[Finding]:
    """The api and tests checks on one interpreter."""
    interpreter: pathlib.Path = sync_env(checkout, python, work)
    ensure_deps(interpreter, new_wheel, checkout)
    version: str = run(
        [
            str(interpreter),
            '-c',
            'import sys; print("%d.%d" % sys.version_info[:2])',
        ]
    ).stdout.strip()
    old_target: pathlib.Path = work / f'old-{version}'
    new_target: pathlib.Path = work / f'new-{version}'
    for wheel, target in (
        (release.wheel, old_target),
        (new_wheel, new_target),
    ):
        target.mkdir()
        install(interpreter, wheel, target)
    out: list[Finding] = []
    if 'api' not in skip:
        old_api: dict[str, object] = manifest(interpreter, old_target, names)
        new_api: dict[str, object] = manifest(interpreter, new_target, names)
        out.extend(
            compat_rules.diff_manifests(f'api {version}', old_api, new_api)
        )
    if 'tests' not in skip:
        if tests is None:
            out.append(
                Finding(
                    'skip',
                    f'tests {version}',
                    'old test suite',
                    'the old source has no tests',
                )
            )
        else:
            out.extend(
                test_findings(
                    interpreter,
                    tests,
                    old_target,
                    new_target,
                    f'tests {version}',
                )
            )
    return out


def tag_exists(checkout: pathlib.Path, tag: str) -> bool:
    """True when the tag is known to the local repository."""
    return (
        run(
            ['git', 'rev-parse', '-q', '--verify', f'refs/tags/{tag}'],
            cwd=checkout,
        ).returncode
        == 0
    )


def griffe_findings(
    checkout: pathlib.Path, tag: str, names: list[str], known: list[Finding]
) -> list[Finding]:
    """Static API breakages from griffe that the runtime check did not already report."""  # noqa: E501
    if not tag_exists(checkout, tag):
        return [
            Finding('skip', 'griffe', tag, 'tag not in the local repository')
        ]
    # griffe maps the search path into its worktree of the old tag, which only
    # works for a path relative to the checkout. An absolute one loads the
    # working tree twice and reports nothing.
    search: str = 'src' if (checkout / 'src').is_dir() else '.'
    seen: set[str] = {
        f.subject.removesuffix('()')
        for f in known
        if f.severity in ('break', 'warn')
    }
    out: list[Finding] = []
    for name in names:
        done: subprocess.CompletedProcess[str] = run(
            [
                'uvx',
                '--no-config',
                'griffe',
                'check',
                name,
                '-a',
                tag,
                '-s',
                search,
                '-f',
                'oneline',
                '--no-color',
            ],
            cwd=checkout,
            timeout=600,
        )
        lines: list[str] = [
            line.strip()
            for line in (done.stdout + done.stderr).splitlines()
            if ': ' in line
        ]
        if done.returncode not in (0, 1) or (
            done.returncode == 1 and not lines
        ):
            out.append(
                Finding(
                    'skip',
                    'griffe',
                    name,
                    ' | '.join(done.stderr.strip().splitlines()[-2:]),
                )
            )
            continue
        repeated: int = 0
        for line in lines:
            subject, message = griffe_line(line)
            if subject in seen:
                repeated += 1
            else:
                out.append(Finding('warn', 'griffe', subject, message))
        # Say what griffe found even when every finding repeats the api check,
        # so an empty griffe section never reads as a check that did not run.
        out.append(
            Finding(
                'info',
                'griffe',
                name,
                f'{len(lines)} findings, {repeated} already reported by the api check',  # noqa: E501
            )
        )
    return out


def griffe_line(line: str) -> tuple[str, str]:
    """Split a griffe oneline finding into a dotted object path and its message."""  # noqa: E501
    location, _, rest = line.partition(': ')
    obj, _, message = rest.partition(': ')
    module: str = (
        location.split(':')[0]
        .removesuffix('.py')
        .removesuffix('/__init__')
        .replace('/', '.')
    )
    module = module.removeprefix('src.')
    return (
        f'{module}.{obj.split("(")[0]}',
        f'{obj}: {message}' if '(' in obj else message,
    )


def merge_versions(findings: list[Finding]) -> list[Finding]:
    """One line per finding, with the Python versions it showed up on joined."""  # noqa: E501
    merged: dict[tuple[str, str, str, str], list[str]] = {}
    for finding in findings:
        kind, _, version = finding.source.partition(' ')
        merged.setdefault(
            (finding.severity, kind, finding.subject, finding.message), []
        ).append(version)
    out: list[Finding] = []
    for (severity, kind, subject, message), versions in merged.items():
        label: str = f'{kind} {",".join(v for v in versions if v)}'.strip()
        out.append(Finding(severity, label, subject, message))
    order: dict[str, int] = {
        'break': 0,
        'warn': 1,
        'skip': 2,
        'info': 3,
        'add': 4,
    }
    return sorted(
        out,
        key=lambda f: (
            order.get(f.severity, 9),
            f.source,
            f.subject,
            f.message,
        ),
    )


def resolve_release(
    args: argparse.Namespace,
    checkout: pathlib.Path,
    name: str,
    work: pathlib.Path,
) -> Release:
    """The old release, from PyPI or the tag as asked."""
    tag: str = args.tag or (
        f'v{args.against}'
        if tag_exists(checkout, f'v{args.against}')
        else args.against
    )
    args.tag = tag
    if args.source in ('auto', 'pypi'):
        release: Release | None = old_from_pypi(name, args.against, work)
        if release is not None:
            return release
        if args.source == 'pypi':
            raise SetupError(
                f'{name}=={args.against} has no pure wheel and sdist on PyPI'
            )
    if not tag_exists(checkout, tag):
        raise SetupError(f'no tag {tag} to build the old release from')
    return old_from_tag(tag, checkout, work)


def check(
    args: argparse.Namespace, checkout: pathlib.Path, work: pathlib.Path
) -> tuple[str, list[Finding]]:
    """Run every check that was not skipped."""
    name: str = tomllib.loads((checkout / 'pyproject.toml').read_text())[
        'project'
    ]['name']
    skip: set[str] = set(filter(None, args.skip.split(',')))
    release: Release = resolve_release(args, checkout, name, work)
    new_wheel: pathlib.Path = build_new_wheel(checkout, work)
    names: list[str] = sorted(
        set(top_level_names(release.wheel)) | set(top_level_names(new_wheel))
    )
    findings: list[Finding] = []
    for gone in sorted(
        set(top_level_names(release.wheel)) - set(top_level_names(new_wheel))
    ):
        findings.append(  # noqa: PERF401
            Finding(
                'break',
                'metadata',
                gone,
                'top-level package no longer installed',
            )
        )
    if 'metadata' not in skip:
        findings.extend(
            compat_rules.diff_metadata(
                wheel_metadata(release.wheel), wheel_metadata(new_wheel)
            )
        )
    old_names: list[str] = top_level_names(release.wheel)
    tests: pathlib.Path | None = (
        prepare_old_tests(release, names, work)
        if 'tests' not in skip
        else None
    )
    for python in args.python or [None]:
        findings.extend(
            per_python(
                checkout,
                python,
                release,
                new_wheel,
                old_names,
                skip,
                work,
                tests,
            )
        )
    if 'griffe' not in skip:
        findings.extend(
            griffe_findings(checkout, args.tag, old_names, findings)
        )
    return release.origin, merge_versions(findings)


def report(origin: str, findings: list[Finding]) -> None:
    """Print the findings, breaks first."""
    print(f'compatibility of the working tree against {origin}\n')  # noqa: T201
    for finding in findings:
        print(  # noqa: T201
            f'{finding.severity.upper():5} [{finding.source}] {finding.subject}: {finding.message}'  # noqa: E501
        )
    counts: dict[str, int] = {
        s: sum(f.severity == s for f in findings)
        for s in ('break', 'warn', 'add', 'skip')
    }
    print(  # noqa: T201
        f'\n{counts["break"]} breaks, {counts["warn"]} warnings, {counts["add"]} additions, {counts["skip"]} skipped'  # noqa: E501
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the command line."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=__doc__.split('\n\n')[0]
    )
    parser.add_argument(
        '--against',
        required=True,
        help='released version to compare with, e.g. 4.1.0',
    )
    parser.add_argument(
        '--from',
        dest='source',
        choices=('auto', 'pypi', 'tag'),
        default='auto',
        help='where the old release comes from, default: PyPI when it has it, else the tag',  # noqa: E501
    )
    parser.add_argument(
        '--tag',
        help='git tag of the old release, default: v<version> or <version>',
    )
    parser.add_argument(
        '--python',
        action='append',
        help='interpreter version to check on, repeatable',
    )
    parser.add_argument(
        '--skip', default='', help='comma separated: api,tests,metadata,griffe'
    )
    parser.add_argument('--json', help='also write the findings to this file')
    parser.add_argument(
        '--keep',
        action='store_true',
        help='keep the work directory for inspection',
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """Run the checks and report. Exit 1 on any break, 2 when the check could not run."""  # noqa: E501
    args: argparse.Namespace = parse_args(argv)
    checkout: pathlib.Path = pathlib.Path.cwd()
    work: pathlib.Path = pathlib.Path(tempfile.mkdtemp(prefix='check-compat-'))
    try:
        origin, findings = check(args, checkout, work)
    except (SetupError, subprocess.TimeoutExpired, OSError) as error:
        print(f'check could not run: {error}', file=sys.stderr)  # noqa: T201
        return 2
    finally:
        if args.keep:
            # Run from the work directory: from the checkout, the current
            # directory comes before PYTHONPATH and imports the new code.
            print(  # noqa: T201
                f'work directory kept at {work}: old-X.Y and new-X.Y hold the two releases, run either as '  # noqa: E501
                f'cd {work} && PYTHONPATH=old-X.Y venv-*/bin/python -c "..."',
                file=sys.stderr,
            )
        else:
            shutil.rmtree(work, ignore_errors=True)
    report(origin, findings)
    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps([dataclasses.asdict(f) for f in findings], indent=1)
            + '\n'
        )
    return 1 if any(f.severity == 'break' for f in findings) else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
