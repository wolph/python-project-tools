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
        assert '${{ inputs.' not in step.get('run', '')


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
        return subprocess.run(
            [sys.executable, '-I', '-'],
            input=plan_script(),
            cwd=self.tmp,
            env={
                'TAG': 'v1.0',
                'PACKAGE': 'pkg',
                'PYPI_JSON_URL': pypi.as_uri(),
                'ASSETS_JSON': str(assets_json),
                'EXISTING_DIR': str(self.existing),
                'DIST_DIR': str(self.tmp / 'dist'),
                'GITHUB_OUTPUT': str(self.output),
            },
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
