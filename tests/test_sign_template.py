"""Tests for templates/sign.yml, the workflow that signs a release."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

TEMPLATE: Path = Path(__file__).parent.parent / 'templates' / 'sign.yml'
TEXT: str = TEMPLATE.read_text(encoding='utf-8')
DOC: dict[Any, Any] = yaml.safe_load(TEXT)
STEPS: list[dict[str, Any]] = DOC['jobs']['sign']['steps']


# The workflow steps run in bash on ubuntu-latest, so their shell tests
# need a POSIX shell and POSIX paths, which the Windows runners lack.
POSIX_ONLY: pytest.MarkDecorator = pytest.mark.skipif(
    sys.platform == 'win32', reason='sign.yml runs in bash on Linux'
)


def step_run(name: str) -> str:
    for step in STEPS:
        if step.get('id') == name or step.get('name') == name:
            run: str = step['run']
            return run
    raise KeyError(name)


def plan_script() -> str:
    run: str = step_run('plan')
    match: re.Match[str] | None = re.search(
        r'# ppt-plan-begin\n(.*)# ppt-plan-end', run, re.DOTALL
    )
    assert match is not None
    script: str = match.group(1)
    return script


def test_template_triggers_and_permissions() -> None:
    on: dict[str, Any] = DOC[True]
    assert list(on) == ['workflow_dispatch']
    inputs: dict[str, Any] = on['workflow_dispatch']['inputs']
    assert inputs['tag']['required'] is True
    assert inputs['package']['required'] is True
    assert inputs['request_id']['required'] is False
    assert DOC['permissions'] == {}
    assert DOC['jobs']['sign']['permissions'] == {
        'contents': 'write',
        'id-token': 'write',
    }
    assert DOC['concurrency'] == {
        'group': 'sign-${{ inputs.tag }}',
        'cancel-in-progress': False,
    }
    assert DOC['name'] == 'Sign release'
    assert DOC['run-name'] == 'Sign ${{ inputs.tag }} ${{ inputs.request_id }}'


def test_template_never_clobbers() -> None:
    assert '--clobber' not in TEXT


def test_template_pins_actions_by_sha() -> None:
    uses: list[str] = re.findall(
        r'^\s*(?:- )?uses:\s*(\S+)', TEXT, re.MULTILINE
    )
    assert uses
    for value in uses:
        assert re.fullmatch(r'[\w.-]+/[\w.-]+@[0-9a-f]{40}', value), value


def test_template_inputs_only_via_env() -> None:
    for step in STEPS:
        assert '${{' not in step.get('run', '')


def test_gh_token_only_on_gh_steps() -> None:
    assert 'GH_TOKEN' not in DOC['jobs']['sign']['env']
    for step in STEPS:
        if step.get('id') == 'plan' or 'sigstore' in step.get('uses', ''):
            assert 'GH_TOKEN' not in step.get('env', {})
            assert 'GH_TOKEN' not in step.get('with', {})
    for name in (
        'Read the release',
        'Download the files already on the release',
        'Upload to the release',
    ):
        step: dict[str, Any] = next(s for s in STEPS if s.get('name') == name)
        assert step['env']['GH_TOKEN'] == '${{ github.token }}'


def test_actionlint_clean() -> None:
    actionlint: str | None = shutil.which('actionlint')
    if actionlint is None:
        pytest.skip('actionlint is not installed')
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [actionlint, str(TEMPLATE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@POSIX_ONLY
def test_input_validation_rejects_injection(tmp_path: Path) -> None:
    script: str = step_run('Validate inputs')
    result: subprocess.CompletedProcess[str] = subprocess.run(
        ['bash', '-c', script],
        cwd=tmp_path,
        env={
            'TAG': 'v1;touch pwned',
            'PACKAGE': 'pkg',
            'PATH': '/usr/bin:/bin',
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert not (tmp_path / 'pwned').exists()


@POSIX_ONLY
def test_input_validation_accepts_normal(tmp_path: Path) -> None:
    script: str = step_run('Validate inputs')
    result: subprocess.CompletedProcess[str] = subprocess.run(
        ['bash', '-c', script],
        cwd=tmp_path,
        env={
            'TAG': 'v1.2.3rc1+x',
            'PACKAGE': 'my_pkg-2.x',
            'PATH': '/usr/bin:/bin',
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0


FILE_A: bytes = b'wheel bytes'
FILE_B: bytes = b'sdist bytes'


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Plan:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp: Path = tmp_path
        self.src: Path = tmp_path / 'src'
        self.src.mkdir()
        (self.src / 'a.whl').write_bytes(FILE_A)
        (self.src / 'b.tar.gz').write_bytes(FILE_B)
        self.existing: Path = tmp_path / 'existing'
        self.existing.mkdir()
        self.output: Path = tmp_path / 'output'
        self.output.touch()

    def run(
        self,
        assets: list[str],
        urls: list[dict[str, Any]] | None = None,
        files_prefix: str | None = 'file://',
    ) -> subprocess.CompletedProcess[str]:
        if urls is None:
            urls = [
                {
                    'filename': name,
                    'url': (self.src / name).as_uri(),
                    'digests': {'sha256': sha((self.src / name).read_bytes())},
                }
                for name in ('a.whl', 'b.tar.gz')
            ]
        pypi: Path = self.tmp / 'pypi.json'
        pypi.write_text(json.dumps({'urls': urls}))
        assets_json: Path = self.tmp / 'assets.json'
        assets_json.write_text(
            json.dumps({'assets': [{'name': n} for n in assets]})
        )
        env: dict[str, str] = {
            'TAG': 'v1.0',
            'PACKAGE': 'pkg',
            'PYPI_JSON_URL': pypi.as_uri(),
            'ASSETS_JSON': str(assets_json),
            'EXISTING_DIR': str(self.existing),
            'DIST_DIR': str(self.tmp / 'dist'),
            'GITHUB_OUTPUT': str(self.output),
        }
        if files_prefix is not None:
            env['PYPI_FILES_PREFIX'] = files_prefix
        return subprocess.run(
            [shutil.which('python3') or sys.executable, '-I', '-'],
            input=plan_script(),
            cwd=self.tmp,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def outputs(self) -> dict[str, str]:
        pairs: dict[str, str] = {}
        for line in self.output.read_text().splitlines():
            key, _, value = line.partition('=')
            pairs[key] = value
        return pairs


def test_plan_downloads_and_lists(tmp_path: Path) -> None:
    plan: Plan = Plan(tmp_path)
    result: subprocess.CompletedProcess[str] = plan.run([])
    assert result.returncode == 0, result.stderr
    outputs: dict[str, str] = plan.outputs()
    assert set(outputs['to_sign'].split()) == {'a.whl', 'b.tar.gz'}
    assert set(outputs['to_upload'].split()) == {'a.whl', 'b.tar.gz'}
    assert (tmp_path / 'dist' / 'a.whl').read_bytes() == FILE_A


def test_plan_skips_bundled_and_present(tmp_path: Path) -> None:
    plan: Plan = Plan(tmp_path)
    (plan.existing / 'a.whl').write_bytes(FILE_A)
    result: subprocess.CompletedProcess[str] = plan.run(
        ['a.whl', 'a.whl.sigstore.json']
    )
    assert result.returncode == 0, result.stderr
    outputs: dict[str, str] = plan.outputs()
    assert outputs['to_sign'] == 'b.tar.gz'
    assert outputs['to_upload'] == 'b.tar.gz'


def test_plan_fails_on_pypi_hash_mismatch(tmp_path: Path) -> None:
    plan: Plan = Plan(tmp_path)
    urls: list[dict[str, Any]] = [
        {
            'filename': 'a.whl',
            'url': (plan.src / 'a.whl').as_uri(),
            'digests': {'sha256': sha(b'other')},
        }
    ]
    result: subprocess.CompletedProcess[str] = plan.run([], urls)
    assert result.returncode == 1
    assert 'a.whl' in result.stderr


def test_plan_fails_on_release_asset_mismatch(tmp_path: Path) -> None:
    plan: Plan = Plan(tmp_path)
    (plan.existing / 'a.whl').write_bytes(b'tampered')
    result: subprocess.CompletedProcess[str] = plan.run(['a.whl'])
    assert result.returncode == 1
    assert 'a.whl' in result.stderr


def test_plan_fails_without_files(tmp_path: Path) -> None:
    plan: Plan = Plan(tmp_path)
    result: subprocess.CompletedProcess[str] = plan.run([], [])
    assert result.returncode == 1


@pytest.mark.parametrize('name', ['a b.whl', 'a\nb.whl', 'a*.whl', '../a.whl'])
def test_plan_rejects_unsafe_file_names(tmp_path: Path, name: str) -> None:
    plan: Plan = Plan(tmp_path)
    urls: list[dict[str, Any]] = [
        {
            'filename': name,
            'url': (plan.src / 'a.whl').as_uri(),
            'digests': {'sha256': sha(FILE_A)},
        }
    ]
    result: subprocess.CompletedProcess[str] = plan.run([], urls)
    assert result.returncode == 1
    assert 'unsafe' in result.stderr


@pytest.mark.parametrize(
    'url',
    [
        None,
        'https://evil.example/a.whl',
        'https://files.pythonhosted.org.evil.example/a.whl',
    ],
    ids=['file', 'other host', 'prefix as host'],
)
def test_plan_refuses_urls_outside_pypi_files(
    tmp_path: Path, url: str | None
) -> None:
    plan: Plan = Plan(tmp_path)
    urls: list[dict[str, Any]] = [
        {
            'filename': 'a.whl',
            'url': url or (plan.src / 'a.whl').as_uri(),
            'digests': {'sha256': sha(FILE_A)},
        }
    ]
    result: subprocess.CompletedProcess[str] = plan.run(
        [], urls, files_prefix=None
    )
    assert result.returncode == 1
    assert result.stderr.strip() == (
        'Refusing a.whl: its URL does not start with '
        'https://files.pythonhosted.org/'
    )
    assert not (tmp_path / 'dist' / 'a.whl').exists()


STUB_GH: str = """#!/bin/sh
printf '%s\\n' "$@" >> "$STUB_LOG"
printf -- '--\\n' >> "$STUB_LOG"
if [ -n "$STUB_FAIL" ]; then
  echo "gh: boom" >&2
  exit 1
fi
"""


def run_step(
    tmp_path: Path,
    name: str,
    env: dict[str, str],
    fail: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    bin_dir: Path = tmp_path / 'bin'
    bin_dir.mkdir(exist_ok=True)
    stub: Path = bin_dir / 'gh'
    stub.write_text(STUB_GH)
    stub.chmod(0o755)
    log: Path = tmp_path / 'gh.log'
    log.touch()
    python3: str = shutil.which('python3') or sys.executable
    full_env: dict[str, str] = {
        'PATH': f'{bin_dir}:{Path(python3).parent}:/usr/bin:/bin',
        'STUB_LOG': str(log),
        'TAG': 'v1.0',
        **env,
    }
    if fail:
        full_env['STUB_FAIL'] = '1'
    result: subprocess.CompletedProcess[str] = subprocess.run(
        ['bash', '-c', step_run(name)],
        cwd=tmp_path,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )
    calls: list[list[str]] = []
    current: list[str] = []
    for line in log.read_text().splitlines():
        if line == '--':
            calls.append(current)
            current = []
        else:
            current.append(line)
    return result, calls


DOWNLOAD: str = 'Download the files already on the release'


def write_assets(tmp_path: Path, names: list[str]) -> None:
    (tmp_path / 'assets.json').write_text(
        json.dumps({'assets': [{'name': n} for n in names]})
    )


@POSIX_ONLY
@pytest.mark.parametrize('names', [[], ['notes.txt', 'SHA256SUMS']])
def test_download_skipped_without_dists(
    tmp_path: Path, names: list[str]
) -> None:
    write_assets(tmp_path, names)
    result, calls = run_step(tmp_path, DOWNLOAD, {})
    assert result.returncode == 0, result.stderr
    assert calls == []
    assert (tmp_path / 'existing').is_dir()


@POSIX_ONLY
def test_download_runs_with_patterns(tmp_path: Path) -> None:
    write_assets(tmp_path, ['a.whl', 'a.whl.sigstore.json'])
    result, calls = run_step(tmp_path, DOWNLOAD, {})
    assert result.returncode == 0, result.stderr
    assert calls == [
        [
            'release',
            'download',
            'v1.0',
            '-D',
            'existing',
            '-p',
            '*.whl',
            '-p',
            '*.tar.gz',
            '-p',
            '*.zip',
        ]
    ]


@POSIX_ONLY
def test_download_failure_fails_step(tmp_path: Path) -> None:
    write_assets(tmp_path, ['a.tar.gz'])
    result, _ = run_step(tmp_path, DOWNLOAD, {}, fail=True)
    assert result.returncode != 0


@POSIX_ONLY
def test_list_files_to_sign(tmp_path: Path) -> None:
    out: Path = tmp_path / 'out'
    out.touch()
    result, _ = run_step(
        tmp_path,
        'List files to sign',
        {'TO_SIGN': 'a.whl b.tar.gz', 'GITHUB_OUTPUT': str(out)},
    )
    assert result.returncode == 0, result.stderr
    assert out.read_text() == 'sign_inputs=dist/a.whl dist/b.tar.gz\n'


@POSIX_ONLY
def test_upload_argv(tmp_path: Path) -> None:
    result, calls = run_step(
        tmp_path,
        'Upload to the release',
        {'TO_UPLOAD': 'b.tar.gz c.whl', 'TO_SIGN': 'a.whl b.tar.gz'},
    )
    assert result.returncode == 0, result.stderr
    assert calls == [
        [
            'release',
            'upload',
            'v1.0',
            'dist/b.tar.gz',
            'dist/c.whl',
            'dist/a.whl.sigstore.json',
            'dist/b.tar.gz.sigstore.json',
        ]
    ]
    assert '--clobber' not in calls[0]


@POSIX_ONLY
def test_upload_nothing_skips_gh(tmp_path: Path) -> None:
    result, calls = run_step(
        tmp_path, 'Upload to the release', {'TO_UPLOAD': '', 'TO_SIGN': ''}
    )
    assert result.returncode == 0, result.stderr
    assert calls == []


@POSIX_ONLY
def test_upload_failure_fails_step(tmp_path: Path) -> None:
    result, _ = run_step(
        tmp_path,
        'Upload to the release',
        {'TO_UPLOAD': 'a.whl', 'TO_SIGN': ''},
        fail=True,
    )
    assert result.returncode != 0
