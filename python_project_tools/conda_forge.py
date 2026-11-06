#!/usr/bin/env python3
"""The conda-forge side of a release: find the feedstock, prepare the bump, verify.

The regro autotick bot opens a version PR on the feedstock a few hours after
PyPI has the sdist, and only touches the version and the checksum. Everything
else the recipe gets wrong (a build backend missing from ``host``, a run
dependency the package grew, a renamed sdist) turns that PR red and leaves it
open. This script does the deterministic part of fixing that so the skill can
merge the release the same day.

Subcommands of ``ppt conda-forge``::

    status  --version 4.1.0 [--package python-utils] [--feedstock OWNER/REPO]
            [--json out.json]
    prepare --version 4.1.0 --dir ../python-utils-feedstock [--pyproject pyproject.toml]
            [--dry-run]
    verify  --version 4.1.0 [--package python-utils] [--conda-name python-utils]
            [--wait 900]

``status`` is read only. ``prepare`` edits the recipe in a feedstock checkout
and prints every edit it made and every difference it did not dare to fix.
``verify`` polls anaconda.org until the version is there. Only the standard
library, ``gh`` and ``git`` are needed. Run it as ``ppt conda-forge``.
"""  # noqa: E501

from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import re
import subprocess
import sys
import time

from python_project_tools import common
from python_project_tools.toml_compat import tomllib

BOT_LOGIN: str = 'regro-cf-autotick-bot'
MAPPING_URL: str = (
    'https://raw.githubusercontent.com/regro/cf-graph-countyfair/master/'
    'mappings/pypi/grayskull_pypi_mapping.json'
)
BOT_TITLE: re.Pattern[str] = re.compile(
    r'^(?:\[bot-automerge\] )?(\S+) v(\S+)$'
)
# PyPI build backend module -> conda-forge package name.
BACKENDS: dict[str, str] = {
    'uv_build': 'uv-build',
    'hatchling.build': 'hatchling',
    'flit_core.buildapi': 'flit-core',
    'setuptools.build_meta': 'setuptools',
    'poetry.core.masonry.api': 'poetry-core',
    'pdm.backend': 'pdm-backend',
    'maturin': 'maturin',
    'scikit_build_core.build': 'scikit-build-core',
    'mesonpy': 'meson-python',
}


@dataclasses.dataclass
class Sdist:
    """The sdist of a version on PyPI."""

    filename: str
    url: str
    sha256: str


@dataclasses.dataclass
class Requirement:
    """A dependency as ``name`` plus a version spec such as ``>=1.24``."""

    name: str
    spec: str


@dataclasses.dataclass
class Recipe:
    """What a conda-forge recipe says, parsed from its text."""

    filename: str
    text: str
    style: str  # 'meta' for meta.yaml, 'v1' for recipe.yaml
    name: str
    version: str
    sha256: str
    build_number: int
    url: str
    host: list[Requirement]
    run: list[Requirement]
    entry_points: list[str]
    maintainers: list[str]


@dataclasses.dataclass
class BotPR:
    """An open version PR from the autotick bot."""

    number: int
    title: str
    version: str
    head: str
    url: str
    created: str


class TemplateError(ValueError):
    """A url template uses an expression this script does not render."""


# --- helpers -----------------------------------------------------------------


def normalise(name: str) -> str:
    """Compare package names the way PyPI does: lower case, one separator."""
    return re.sub(r'[-_.]+', '-', name).lower()


def version_key(version: str) -> tuple[tuple[int, str], ...]:
    """Sort key that orders 4.10.0 after 4.9.1 and tolerates letters."""
    parts: list[tuple[int, str]] = []
    for part in re.split(r'[.\-]', version):
        digits: re.Match[str] | None = re.match(r'(\d+)(.*)', part)
        parts.append(
            (int(digits.group(1)), digits.group(2)) if digits else (-1, part)
        )
    return tuple(parts)


def parse_requirement(line: str) -> Requirement:
    """Split ``numpy >=1.24`` or ``numpy>=1.24,<2`` into name and spec."""
    match: re.Match[str] | None = re.match(
        r'\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*)$', line
    )
    if match is None:
        return Requirement(line.strip(), '')
    return Requirement(match.group(1), match.group(2).replace(' ', ''))


# --- PyPI and pyproject ------------------------------------------------------


def pypi_sdist(package: str, version: str) -> Sdist | None:
    """The sdist PyPI serves for a version, or None when it is not there."""
    data: object | None = common.fetch_json(
        f'https://pypi.org/pypi/{package}/{version}/json'
    )
    if not isinstance(data, dict):
        return None
    for entry in data.get('urls', []):
        if entry.get('packagetype') == 'sdist':
            return Sdist(
                entry['filename'], entry['url'], entry['digests']['sha256']
            )
    return None


def pep508_requirements(
    specs: list[str],
) -> tuple[list[Requirement], list[str]]:
    """Dependencies without a marker, and the ones with a marker left to a human."""  # noqa: E501
    plain: list[Requirement] = []
    conditional: list[str] = []
    for spec in specs:
        if ';' in spec:
            conditional.append(spec)
            continue
        bare: str = re.sub(r'\[[^\]]*\]', '', spec)
        plain.append(parse_requirement(bare))
    return plain, conditional


def backend_requirement(pyproject: dict[str, object]) -> Requirement | None:
    """The build backend as a conda requirement, e.g. ``uv-build >=0.11,<0.13``."""  # noqa: E501
    build_system: dict[str, object] = pyproject.get('build-system', {})  # ty: ignore[invalid-assignment]
    backend: str = str(build_system.get('build-backend', ''))
    conda_name: str | None = BACKENDS.get(backend)
    if conda_name is None:
        return None
    for spec in build_system.get('requires', []):  # ty: ignore[not-iterable]
        requirement: Requirement = parse_requirement(str(spec))
        if normalise(requirement.name) == normalise(
            backend.split('.')[0].replace('_', '-')
        ):
            return Requirement(conda_name, requirement.spec)
        if normalise(requirement.name) == normalise(conda_name):
            return Requirement(conda_name, requirement.spec)
    return Requirement(conda_name, '')


def pyproject_scripts(pyproject: dict[str, object]) -> list[str]:
    """``[project.scripts]`` in the ``name = module:func`` form recipes use."""
    project: dict[str, object] = pyproject.get('project', {})  # ty: ignore[invalid-assignment]
    scripts: dict[str, str] = project.get('scripts', {})  # ty: ignore[invalid-assignment]
    return [f'{name} = {target}' for name, target in scripts.items()]


# --- recipe parsing ----------------------------------------------------------


def indent_of(line: str) -> int:
    """Leading spaces of a line."""
    return len(line) - len(line.lstrip(' '))


def block(lines: list[str], start: int) -> list[int]:
    """Indices of the lines nested under the key at ``start``."""
    base: int = indent_of(lines[start])
    found: list[int] = []
    for index in range(start + 1, len(lines)):
        line: str = lines[index]
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if indent_of(line) <= base:
            break
        found.append(index)
    return found


def find_key(
    lines: list[str], key: str, within: list[int] | None = None
) -> int | None:
    """Index of the first ``key:`` line, optionally inside a block."""
    candidates: list[int] = (
        within if within is not None else list(range(len(lines)))
    )
    for index in candidates:
        if re.match(
            rf'\s*{re.escape(key)}:\s*(#.*)?$', lines[index]
        ) or re.match(rf'\s*{re.escape(key)}:\s+\S', lines[index]):
            return index
    return None


def items(lines: list[str], key_index: int) -> list[str]:
    """The ``- item`` values nested under a key, selectors and comments stripped."""  # noqa: E501
    found: list[str] = []
    for index in block(lines, key_index):
        stripped: str = lines[index].strip()
        if stripped.startswith('- '):
            found.append(re.sub(r'\s+#.*$', '', stripped[2:]).strip())
    return found


def scalar(lines: list[str], key: str, within: list[int] | None = None) -> str:
    """The value of ``key: value`` inside a block, quotes removed."""
    index: int | None = find_key(lines, key, within)
    if index is None:
        return ''
    value: str = lines[index].split(':', 1)[1].strip()
    value = re.sub(r'\s+#.*$', '', value)
    return value.strip('\'"')


def recipe_style(filename: str) -> str:
    """``meta`` for the jinja meta.yaml, ``v1`` for the rattler recipe.yaml."""
    return 'v1' if filename.endswith('recipe.yaml') else 'meta'


def jinja_set(text: str, variable: str) -> str:
    """The value of ``{% set variable = "..." %}``."""
    match: re.Match[str] | None = re.search(
        rf'{{%\s*set\s+{variable}\s*=\s*[\'"]([^\'"]*)[\'"]\s*%}}', text
    )
    return match.group(1) if match else ''


def section_items(lines: list[str], section: str, key: str) -> list[str]:
    """Items under ``section: key:``, or an empty list."""
    section_index: int | None = find_key(lines, section)
    if section_index is None:
        return []
    key_index: int | None = find_key(lines, key, block(lines, section_index))
    return items(lines, key_index) if key_index is not None else []


def parse_recipe(filename: str, text: str) -> Recipe:
    """Read the fields the release cares about from a recipe."""
    lines: list[str] = text.splitlines()
    style: str = recipe_style(filename)
    context: int | None = find_key(lines, 'context')
    context_block: list[int] | None = (
        block(lines, context) if context is not None else None
    )
    name: str = (
        jinja_set(text, 'name')
        if style == 'meta'
        else scalar(lines, 'name', context_block)
    )
    version: str = (
        jinja_set(text, 'version')
        if style == 'meta'
        else scalar(lines, 'version', context_block)
    )
    source: int | None = find_key(lines, 'source')
    source_block: list[int] | None = (
        block(lines, source) if source is not None else None
    )
    sha256: str = jinja_set(text, 'sha256') or scalar(
        lines, 'sha256', source_block
    )
    build: int | None = find_key(lines, 'build')
    build_block: list[int] | None = (
        block(lines, build) if build is not None else None
    )
    number: str = scalar(lines, 'number', build_block) or '0'
    entry_index: int | None = find_key(lines, 'entry_points', build_block)
    return Recipe(
        filename=filename,
        text=text,
        style=style,
        name=name,
        version=version,
        sha256=sha256,
        build_number=int(number),
        url=scalar(lines, 'url', source_block),
        host=[
            parse_requirement(item)
            for item in section_items(lines, 'requirements', 'host')
        ],
        run=[
            parse_requirement(item)
            for item in section_items(lines, 'requirements', 'run')
        ],
        entry_points=items(lines, entry_index)
        if entry_index is not None
        else [],
        maintainers=section_items(lines, 'extra', 'recipe-maintainers'),
    )


# --- url templates -----------------------------------------------------------


def render_expression(expression: str, name: str, version: str) -> str:
    """Evaluate the handful of jinja expressions feedstock urls use."""
    parts: list[str] = [part.strip() for part in expression.split('|')]
    base: str = parts[0]
    value: str
    if base == 'name':
        value = name
    elif base == 'version':
        value = version
    elif base == 'name[0]':
        value = name[0]
    else:
        replace: re.Match[str] | None = re.match(
            r'name\.replace\([\'"]([^\'"]*)[\'"]\s*,\s*[\'"]([^\'"]*)[\'"]\)$',
            base,
        )
        if replace is None:
            raise TemplateError(expression)
        value = name.replace(replace.group(1), replace.group(2))
    for filter_name in parts[1:]:
        if filter_name == 'lower':
            value = value.lower()
        else:
            raise TemplateError(expression)
    return value


def render(template: str, name: str, version: str) -> str:
    """Render ``{{ ... }}`` and ``${{ ... }}`` placeholders in a url."""
    return re.sub(
        r'\$?\{\{\s*(.*?)\s*\}\}',
        lambda match: render_expression(match.group(1), name, version),
        template,
    )


def filename_template(
    filename: str, name: str, version: str, style: str
) -> str:
    """Turn ``python_utils-4.1.2.tar.gz`` back into a template for the recipe."""  # noqa: E501
    opener: str = '${{ ' if style == 'v1' else '{{ '
    stem: str = filename[: filename.index(version)].rstrip('-_')
    if stem == name.replace('-', '_') and stem != name:
        stem_template: str = f"{opener}name.replace('-', '_') }}}}"
    elif stem == name:
        stem_template = f'{opener}name }}}}'
    else:
        stem_template = stem
    separator: str = filename[len(stem)]
    return f'{stem_template}{separator}{opener}version }}}}{filename[filename.index(version) + len(version) :]}'  # noqa: E501


# --- edits -------------------------------------------------------------------


def replace_once(
    text: str, pattern: str, replacement: str
) -> tuple[str, bool]:
    """Substitute the first match and say whether anything changed."""
    new_text: str
    count: int
    new_text, count = re.subn(
        pattern, replacement, text, count=1, flags=re.MULTILINE
    )
    return new_text, count > 0 and new_text != text


def bump_version(
    recipe: Recipe, version: str
) -> tuple[str, list[common.Result]]:
    """Set the version, which also means the build number goes back to 0."""
    text: str = recipe.text
    results: list[common.Result] = []
    changed: bool
    if recipe.style == 'meta':
        text, changed = replace_once(
            text,
            r'({%\s*set\s+version\s*=\s*)([\'"])[^\'"]*\2',
            rf'\g<1>\g<2>{version}\g<2>',
        )
    else:
        text, changed = replace_once(
            text,
            r'^(\s+version:\s*)([\'"]?)[^\'"\n]*\2',
            rf'\g<1>\g<2>{version}\g<2>',
        )
    if changed:
        results.append(
            common.Result(
                status='EDIT',
                name='version',
                evidence=f'{recipe.version} -> {version}',
            )
        )
        text, reset = replace_once(text, r'^(\s+number:\s*)\d+', r'\g<1>0')
        if reset and recipe.build_number != 0:
            results.append(
                common.Result(
                    status='EDIT',
                    name='build-number',
                    evidence=f'{recipe.build_number} -> 0',
                )
            )
    elif recipe.version == version:
        results.append(
            common.Result(
                status='INFO',
                name='version',
                evidence=f'recipe already says {version}',
            )
        )
    else:
        results.append(
            common.Result(
                status='WARN',
                name='version',
                evidence=f'could not find the version in {recipe.filename}',
            )
        )
    return text, results


def set_sha256(
    text: str, recipe: Recipe, sdist: Sdist
) -> tuple[str, list[common.Result]]:
    """Point the checksum at the new sdist."""
    if recipe.sha256 == sdist.sha256:
        return text, [
            common.Result(
                status='INFO',
                name='sha256',
                evidence='already matches the sdist',
            )
        ]
    new_text: str
    changed: bool
    new_text, changed = replace_once(
        text,
        r'({%\s*set\s+sha256\s*=\s*)([\'"])[^\'"]*\2',
        rf'\g<1>\g<2>{sdist.sha256}\g<2>',
    )
    if not changed:
        new_text, changed = replace_once(
            text, r'^(\s+sha256:\s*)\S+', rf'\g<1>{sdist.sha256}'
        )
    if not changed:
        return text, [
            common.Result(
                status='WARN',
                name='sha256',
                evidence=f'could not find the checksum in {recipe.filename}',
            )
        ]
    return new_text, [
        common.Result(
            status='EDIT',
            name='sha256',
            evidence=f'{recipe.sha256[:12]} -> {sdist.sha256[:12]}',
        )
    ]


def fix_url(
    text: str, recipe: Recipe, sdist: Sdist, version: str
) -> tuple[str, list[common.Result]]:
    """Make the url template render to the filename PyPI actually serves."""
    name: str = recipe.name or sdist.filename.split('-')[0]
    try:
        rendered: str = render(recipe.url, name, version)
    except TemplateError as error:
        return text, [
            common.Result(
                status='WARN',
                name='url',
                evidence=f'cannot render {error}: check {recipe.url} by hand',
            )
        ]
    if rendered.rsplit('/', 1)[-1] == sdist.filename:
        return text, [
            common.Result(
                status='INFO',
                name='url',
                evidence=f'renders to {sdist.filename}',
            )
        ]
    new_name: str = filename_template(
        sdist.filename, name, version, recipe.style
    )
    new_url: str = recipe.url.rsplit('/', 1)[0] + '/' + new_name
    new_text: str = text.replace(recipe.url, new_url, 1)
    return new_text, [
        common.Result(
            status='EDIT',
            name='url',
            evidence=f'{recipe.url.rsplit("/", 1)[-1]} -> {new_name}',
        )
    ]


def add_backend(
    text: str, recipe: Recipe, backend: Requirement | None
) -> tuple[str, list[common.Result]]:
    """Add the build backend to ``host`` when pip would otherwise not find it."""  # noqa: E501
    if backend is None:
        return text, [
            common.Result(
                status='WARN',
                name='backend',
                evidence=(
                    'unknown build backend in pyproject.toml, '
                    'check host by hand'
                ),
            )
        ]
    host_names: set[str] = {normalise(item.name) for item in recipe.host}
    wanted: str = f'{backend.name} {backend.spec}'.strip()
    if normalise(backend.name) in host_names:
        current: Requirement = next(
            item
            for item in recipe.host
            if normalise(item.name) == normalise(backend.name)
        )
        if current.spec != backend.spec:
            return text, [
                common.Result(
                    status='WARN',
                    name='backend',
                    evidence=(
                        f'host pins {current.name} {current.spec}, pyproject '
                        f'says {backend.spec}'
                    ),
                )
            ]
        return text, [
            common.Result(
                status='INFO', name='backend', evidence=f'host has {wanted}'
            )
        ]
    lines: list[str] = text.splitlines(keepends=True)
    requirements: int | None = find_key(
        [line.rstrip('\n') for line in lines], 'requirements'
    )
    host: int | None = (
        find_key(
            [line.rstrip('\n') for line in lines],
            'host',
            block([line.rstrip('\n') for line in lines], requirements),
        )
        if requirements is not None
        else None
    )
    if host is None:
        return text, [
            common.Result(
                status='WARN',
                name='backend',
                evidence=f'no requirements.host section to add {wanted} to',
            )
        ]
    host_items: list[int] = [
        index
        for index in block([line.rstrip('\n') for line in lines], host)
        if lines[index].strip().startswith('- ')
    ]
    if not host_items:
        return text, [
            common.Result(
                status='WARN',
                name='backend',
                evidence=f'empty host section, add {wanted} by hand',
            )
        ]
    pip_items: list[int] = [
        index
        for index in host_items
        if lines[index].strip().startswith('- pip')
    ]
    anchor: int = pip_items[-1] if pip_items else host_items[-1]
    indent: str = ' ' * indent_of(lines[anchor])
    lines.insert(anchor + 1, f'{indent}- {wanted}\n')
    return ''.join(lines), [
        common.Result(
            status='EDIT', name='backend', evidence=f'added {wanted} to host'
        )
    ]


# --- comparisons -------------------------------------------------------------


def conda_name_for(pypi_name: str, mapping: dict[str, dict[str, str]]) -> str:
    """The conda-forge name of a PyPI package, from the regro mapping when known."""  # noqa: E501
    entry: dict[str, str] | None = mapping.get(normalise(pypi_name))
    return entry['conda_name'] if entry else pypi_name.lower()


def compare_run(
    pyproject: dict[str, object],
    recipe: Recipe,
    mapping: dict[str, dict[str, str]],
) -> list[common.Result]:
    """Run dependencies in pyproject.toml against the recipe's run section."""
    project: dict[str, object] = pyproject.get('project', {})  # ty: ignore[invalid-assignment]
    plain: list[Requirement]
    conditional: list[str]
    plain, conditional = pep508_requirements(
        list(project.get('dependencies', []))  # ty: ignore[invalid-argument-type]
    )  # type: ignore[arg-type]
    results: list[common.Result] = []
    recipe_run: dict[str, Requirement] = {
        normalise(item.name): item
        for item in recipe.run
        if normalise(item.name) != 'python'
    }
    for requirement in plain:
        conda: str = conda_name_for(requirement.name, mapping)
        found: Requirement | None = recipe_run.pop(
            normalise(conda), None
        ) or recipe_run.pop(normalise(requirement.name), None)
        if found is None:
            results.append(
                common.Result(
                    status='WARN',
                    name='run',
                    evidence=(
                        f'pyproject needs {requirement.name} '
                        f'{requirement.spec}, recipe run lacks {conda}'
                    ),
                )
            )
        elif found.spec != requirement.spec:
            results.append(
                common.Result(
                    status='WARN',
                    name='run',
                    evidence=(
                        f'{found.name}: recipe pins '
                        f'{found.spec or "nothing"}, pyproject '
                        f'{requirement.spec or "nothing"}'
                    ),
                )
            )
        else:
            results.append(
                common.Result(
                    status='INFO',
                    name='run',
                    evidence=f'{found.name} {found.spec} matches'.rstrip(),
                )
            )
    extras: dict[str, list[str]] = project.get('optional-dependencies', {})  # ty: ignore[invalid-assignment]
    for leftover in recipe_run.values():
        groups: list[str] = [
            group
            for group, specs in extras.items()
            if any(
                normalise(parse_requirement(spec.split(';')[0]).name)
                == normalise(leftover.name)
                for spec in specs
            )
        ]
        where: str = (
            f'only in extra {", ".join(groups)}'
            if groups
            else 'pyproject does not depend on it'
        )
        results.append(
            common.Result(
                status='WARN',
                name='run',
                evidence=(
                    'recipe run has '
                    f'{f"{leftover.name} {leftover.spec}".strip()}, {where}'
                ),
            )
        )
    for spec in conditional:
        results.append(  # noqa: PERF401
            common.Result(
                status='WARN',
                name='run',
                evidence=(
                    f'conditional dependency needs a selector by hand: {spec}'
                ),
            )
        )
    return results


def compare_python(
    pyproject: dict[str, object], recipe: Recipe
) -> list[common.Result]:
    """The Python floor in pyproject.toml against the recipe's python requirement."""  # noqa: E501
    project: dict[str, object] = pyproject.get('project', {})  # ty: ignore[invalid-assignment]
    floor: str = str(project.get('requires-python', '')).replace(' ', '')
    python: Requirement | None = next(
        (item for item in recipe.run if normalise(item.name) == 'python'), None
    )
    if python is None:
        return [
            common.Result(
                status='WARN',
                name='python',
                evidence='recipe run has no python requirement',
            )
        ]
    if 'python_min' in python.spec:
        return [
            common.Result(
                status='INFO',
                name='python',
                evidence=(
                    'recipe follows conda-forge python_min, pyproject says '
                    f'{floor or "nothing"}'
                ),
            )
        ]
    if floor and python.spec != floor:
        return [
            common.Result(
                status='WARN',
                name='python',
                evidence=(
                    'recipe run has python '
                    f'{python.spec or "without a floor"}, pyproject '
                    f'requires-python {floor}'
                ),
            )
        ]
    return [
        common.Result(
            status='INFO',
            name='python',
            evidence=f'python {python.spec} matches requires-python',
        )
    ]


def compare_entry_points(
    pyproject: dict[str, object], recipe: Recipe
) -> list[common.Result]:
    """Console scripts in pyproject.toml against the recipe's entry points."""
    wanted: set[str] = {
        entry.replace(' ', '') for entry in pyproject_scripts(pyproject)
    }
    present: set[str] = {
        entry.replace(' ', '') for entry in recipe.entry_points
    }
    results: list[common.Result] = []
    for entry in sorted(wanted - present):
        results.append(  # noqa: PERF401
            common.Result(
                status='WARN',
                name='entry-points',
                evidence=f'pyproject defines {entry}, recipe does not list it',
            )
        )
    for entry in sorted(present - wanted):
        results.append(  # noqa: PERF401
            common.Result(
                status='WARN',
                name='entry-points',
                evidence=f'recipe lists {entry}, pyproject has no such script',
            )
        )
    if wanted and not results:
        results.append(
            common.Result(
                status='INFO',
                name='entry-points',
                evidence=f'{len(wanted)} entry points match',
            )
        )
    return results


def compare_host(
    recipe: Recipe, backend: Requirement | None
) -> list[common.Result]:
    """Leftovers in host that a backend switch tends to leave behind."""
    if backend is None or normalise(backend.name) == 'setuptools':
        return []
    stale: list[str] = [
        item.name
        for item in recipe.host
        if normalise(item.name)
        in {'setuptools', 'wheel', 'setuptools-scm', 'pytest-runner'}
    ]
    return [
        common.Result(
            status='WARN',
            name='host',
            evidence=(
                f'host still lists {name} while the backend is {backend.name}'
            ),
        )
        for name in stale
    ]


def compare(
    pyproject: dict[str, object],
    recipe: Recipe,
    mapping: dict[str, dict[str, str]],
) -> list[common.Result]:
    """Every difference between pyproject.toml and the recipe worth a look."""
    backend: Requirement | None = backend_requirement(pyproject)
    return [
        *compare_run(pyproject, recipe, mapping),
        *compare_python(pyproject, recipe),
        *compare_entry_points(pyproject, recipe),
        *compare_host(recipe, backend),
    ]


# --- GitHub ------------------------------------------------------------------


def load_mapping() -> dict[str, dict[str, str]]:
    """The regro PyPI to conda-forge name mapping, keyed by normalised PyPI name."""  # noqa: E501
    data: object | None = common.fetch_json(MAPPING_URL)
    if not isinstance(data, dict):
        return {}
    return {normalise(key): value for key, value in data.items()}


def find_feedstock(
    package: str, mapping: dict[str, dict[str, str]]
) -> str | None:
    """``conda-forge/<name>-feedstock`` for a PyPI package, or None."""
    candidates: list[str] = [
        package.lower(),
        conda_name_for(package, mapping),
        package.lower().replace('_', '-'),
        re.sub(r'[-_.]', '', package.lower()),
    ]
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        repo: str = f'conda-forge/{candidate}-feedstock'
        if (
            common.gh_json(['api', f'repos/{repo}', '--jq', '{name}'])
            is not None
        ):
            return repo
    return None


def repo_file(repo: str, path: str) -> str | None:
    """The text of a file on the feedstock's default branch."""
    done: subprocess.CompletedProcess[str] = common.run(
        [
            'gh',
            'api',
            f'repos/{repo}/contents/{path}',
            '-H',
            'Accept: application/vnd.github.raw+json',
        ]
    )
    return done.stdout if done.returncode == 0 else None


def fetch_recipe(repo: str) -> Recipe | None:
    """The recipe on the feedstock's default branch, whichever format it uses."""  # noqa: E501
    for filename in ('recipe/meta.yaml', 'recipe/recipe.yaml'):
        text: str | None = repo_file(repo, filename)
        if text is not None:
            return parse_recipe(filename, text)
    return None


def can_push(repo: str) -> bool:
    """Whether the gh user may push to (and so merge on) the feedstock."""
    data: object | None = common.gh_json(
        ['api', f'repos/{repo}', '--jq', '.permissions']
    )
    return isinstance(data, dict) and bool(data.get('push'))


def automerge_enabled(repo: str) -> bool:
    """Whether conda-forge.yml asks the bot to merge green version PRs."""
    text: str | None = repo_file(repo, 'conda-forge.yml')
    return (
        text is not None
        and re.search(
            r'^\s+automerge:\s*(true|version)\s*$', text, flags=re.MULTILINE
        )
        is not None
    )


def bot_prs(repo: str) -> tuple[list[BotPR], list[str]]:
    """Open bot version PRs, and the titles of its other open PRs (migrations)."""  # noqa: E501
    data: object | None = common.gh_json(
        [
            'pr',
            'list',
            '-R',
            repo,
            '--state',
            'open',
            '--author',
            BOT_LOGIN,
            '--limit',
            '50',
            '--json',
            'number,title,headRefName,url,createdAt',
        ]
    )
    versions: list[BotPR] = []
    others: list[str] = []
    for item in data if isinstance(data, list) else []:
        match: re.Match[str] | None = BOT_TITLE.match(item['title'])
        if match is None:
            others.append(f'#{item["number"]} {item["title"]}')
            continue
        versions.append(
            BotPR(
                item['number'],
                item['title'],
                match.group(2),
                item['headRefName'],
                item['url'],
                item['createdAt'][:10],
            )
        )
    return versions, others


def pr_checks(repo: str, number: int) -> str:
    """Counts per outcome of a PR's checks, e.g. ``3 fail, 2 pass, 1 pending``."""  # noqa: E501
    data: object | None = common.gh_json(
        ['pr', 'checks', str(number), '-R', repo, '--json', 'bucket,name']
    )
    if not isinstance(data, list):
        return 'no checks reported'
    counts: dict[str, int] = {}
    for check in data:
        counts[check['bucket']] = counts.get(check['bucket'], 0) + 1
    order: list[str] = ['fail', 'pending', 'pass', 'skipping', 'cancel']
    return (
        ', '.join(
            f'{counts[bucket]} {bucket}'
            for bucket in order
            if bucket in counts
        )
        or 'no checks yet'
    )


def conda_versions(conda_name: str) -> list[str] | None:
    """Versions anaconda.org lists for the conda-forge package, or None when unknown."""  # noqa: E501
    data: object | None = common.fetch_json(
        f'https://api.anaconda.org/package/conda-forge/{conda_name}'
    )
    if not isinstance(data, dict):
        return None
    return [str(version) for version in data.get('versions', [])]


# --- commands ----------------------------------------------------------------


def project_name(pyproject: dict[str, object]) -> str:
    """The ``[project].name`` of a parsed pyproject.toml, or ''."""
    project: object = pyproject.get('project', {})
    if not isinstance(project, dict):
        return ''
    return str(project.get('name', ''))


def read_pyproject(path: pathlib.Path) -> dict[str, object]:
    """Parse pyproject.toml, or an empty dict when it is missing."""
    return tomllib.loads(path.read_text()) if path.exists() else {}


def report(results: list[common.Result]) -> None:
    """Print the table, one line per result."""
    width: int = max((len(result.name) for result in results), default=4)
    for result in results:
        print(f'{result.status:4} {result.name:{width}}  {result.evidence}')  # noqa: T201


def exit_status(results: list[common.Result]) -> int:
    """1 when something failed, 2 when the whole thing was skipped, else 0."""
    statuses: set[str] = {result.status for result in results}
    if 'FAIL' in statuses:
        return 1
    if statuses == {'SKIP'}:
        return 2
    return 0


def stale_prs(prs: list[BotPR], recipe_version: str) -> list[BotPR]:
    """Bot version PRs that main already overtook, which block new ones past three."""  # noqa: E501
    return [
        pr
        for pr in prs
        if version_key(pr.version) <= version_key(recipe_version)
    ]


def sdist_result(package: str, version: str) -> common.Result:
    """Does PyPI serve an sdist for the version."""
    try:
        sdist: Sdist | None = pypi_sdist(package, version)
    except common.FetchError as err:
        return common.Result(status='FAIL', name='sdist', evidence=str(err))
    if sdist is None:
        return common.Result(
            status='FAIL',
            name='sdist',
            evidence=f'PyPI has no sdist for {package} {version} yet',
        )
    return common.Result(status='PASS', name='sdist', evidence=sdist.filename)


def conda_forge_result(conda_name: str, version: str) -> common.Result:
    """Does anaconda.org list the version of the conda-forge package."""
    try:
        listed: list[str] | None = conda_versions(conda_name)
    except common.FetchError as err:
        return common.Result(
            status='FAIL', name='conda-forge', evidence=str(err)
        )
    if listed is None:
        return common.Result(
            status='WARN',
            name='conda-forge',
            evidence=(f'anaconda.org has no package conda-forge/{conda_name}'),
        )
    if version in listed:
        return common.Result(
            status='PASS',
            name='conda-forge',
            evidence=(
                f'https://anaconda.org/conda-forge/{conda_name} '
                f'lists {version}'
            ),
        )
    latest: str = listed[-1] if listed else 'nothing'
    return common.Result(
        status='WARN',
        name='conda-forge',
        evidence=(
            f'anaconda.org latest is {latest}, {version} not uploaded yet'
        ),
    )


def status_feedstock(
    repo: str,
    version: str,
    package: str,
    pyproject: dict[str, object],
    mapping: dict[str, dict[str, str]],
) -> list[common.Result]:
    """Everything the skill needs to decide what to do on the feedstock."""
    results: list[common.Result] = [
        common.Result(
            status='PASS',
            name='feedstock',
            evidence=f'https://github.com/{repo}',
        )
    ]
    recipe: Recipe | None = fetch_recipe(repo)
    if recipe is None:
        return [
            *results,
            common.Result(
                status='FAIL',
                name='recipe',
                evidence=(
                    'no recipe/meta.yaml or recipe/recipe.yaml on the '
                    'default branch'
                ),
            ),
        ]
    results.append(
        common.Result(
            status='INFO',
            name='recipe',
            evidence=(
                f'{recipe.filename} at {recipe.version}, build '
                f'{recipe.build_number}'
            ),
        )
    )
    results.append(
        common.Result(
            status='INFO',
            name='maintainers',
            evidence=', '.join(recipe.maintainers) or 'none listed',
        )
    )
    pushable: bool = can_push(repo)
    results.append(
        common.Result(
            status='PASS' if pushable else 'WARN',
            name='maintainer',
            evidence='you can push and merge'
            if pushable
            else 'you cannot push: only a maintainer or the automerge bot can merge',  # noqa: E501
        )
    )
    automerge: bool = automerge_enabled(repo)
    results.append(
        common.Result(
            status='INFO',
            name='automerge',
            evidence='on: a green bot PR merges itself'
            if automerge
            else 'off: someone has to merge',
        )
    )
    results.append(sdist_result(package, version))
    if version_key(recipe.version) >= version_key(version):
        results.append(
            common.Result(
                status='PASS',
                name='bump',
                evidence=f'main already carries {recipe.version}',
            )
        )
    else:
        results.append(
            common.Result(
                status='WARN',
                name='bump',
                evidence=f'main is at {recipe.version}, needs {version}',
            )
        )
    versions: list[BotPR]
    others: list[str]
    versions, others = bot_prs(repo)
    mine: list[BotPR] = [pr for pr in versions if pr.version == version]
    for pr in mine:
        results.append(  # noqa: PERF401
            common.Result(
                status='INFO',
                name='bot-pr',
                evidence=(
                    f'#{pr.number} {pr.title} ({pr.created}): '
                    f'{pr_checks(repo, pr.number)}, {pr.url}'
                ),
            )
        )
    if not mine:
        results.append(
            common.Result(
                status='INFO',
                name='bot-pr',
                evidence=f'no open bot PR for {version} yet',
            )
        )
    for pr in stale_prs(
        [pr for pr in versions if pr.version != version], recipe.version
    ):
        results.append(  # noqa: PERF401
            common.Result(
                status='WARN',
                name='stale-pr',
                evidence=(
                    f'#{pr.number} {pr.title} is behind main '
                    f'({recipe.version}), and more than three open bot PRs '
                    'stop the bot'
                ),
            )
        )
    for title in others:
        results.append(  # noqa: PERF401
            common.Result(status='INFO', name='other-pr', evidence=title)
        )
    if pyproject:
        results.extend(compare(pyproject, recipe, mapping))
    else:
        results.append(
            common.Result(
                status='SKIP',
                name='compare',
                evidence=(
                    'no pyproject.toml here, dependency comparison skipped'
                ),
            )
        )
    conda_name: str = repo.split('/')[1].removesuffix('-feedstock')
    results.append(conda_forge_result(conda_name, version))
    return results


def command_status(args: argparse.Namespace) -> int:
    """Read-only overview of the feedstock for one version."""
    pyproject: dict[str, object] = read_pyproject(pathlib.Path(args.pyproject))
    package: str = args.package or project_name(pyproject)
    if not package:
        print(  # noqa: T201
            'no package name: pass --package or run inside a checkout',
            file=sys.stderr,
        )
        return 2
    mapping: dict[str, dict[str, str]] = {}
    failures: list[common.Result] = []
    try:
        mapping = load_mapping()
    except common.FetchError as err:
        failures.append(
            common.Result(status='FAIL', name='mapping', evidence=str(err))
        )
    repo: str | None = args.feedstock or find_feedstock(package, mapping)
    results: list[common.Result]
    if repo is None:
        results = [
            common.Result(
                status='SKIP',
                name='feedstock',
                evidence=f'no conda-forge feedstock for {package}',
            )
        ]
    else:
        results = status_feedstock(
            repo, args.version, package, pyproject, mapping
        )
    results = [*failures, *results]
    report(results)
    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps(
                [dataclasses.asdict(result) for result in results], indent=2
            )
        )
    return exit_status(results)


def prepare_text(
    recipe: Recipe, version: str, sdist: Sdist, pyproject: dict[str, object]
) -> tuple[str, list[common.Result]]:
    """Apply every deterministic edit to the recipe text."""
    text: str
    results: list[common.Result]
    text, results = bump_version(recipe, version)
    more: list[common.Result]
    text, more = set_sha256(text, recipe, sdist)
    results.extend(more)
    text, more = fix_url(text, recipe, sdist, version)
    results.extend(more)
    if pyproject:
        text, more = add_backend(text, recipe, backend_requirement(pyproject))
        results.extend(more)
    return text, results


def command_prepare(args: argparse.Namespace) -> int:
    """Edit the recipe in a feedstock checkout for the new version."""
    pyproject: dict[str, object] = read_pyproject(pathlib.Path(args.pyproject))
    package: str = args.package or project_name(pyproject)
    recipe_dir: pathlib.Path = pathlib.Path(args.dir) / 'recipe'
    paths: list[pathlib.Path] = [
        path
        for path in (recipe_dir / 'meta.yaml', recipe_dir / 'recipe.yaml')
        if path.exists()
    ]
    if not paths or not package:
        print(  # noqa: T201
            f'need a recipe under {recipe_dir} and a package name',
            file=sys.stderr,
        )
        return 2
    recipe: Recipe = parse_recipe(str(paths[0]), paths[0].read_text())
    sdist: Sdist | None
    try:
        sdist = pypi_sdist(package, args.version)
    except common.FetchError as err:
        report([common.Result(status='FAIL', name='sdist', evidence=str(err))])
        return 1
    if sdist is None:
        report(
            [
                common.Result(
                    status='FAIL',
                    name='sdist',
                    evidence=(
                        f'PyPI has no sdist for {package} {args.version} yet'
                    ),
                )
            ]
        )
        return 1
    text: str
    results: list[common.Result]
    text, results = prepare_text(recipe, args.version, sdist, pyproject)
    if pyproject:
        try:
            mapping: dict[str, dict[str, str]] = load_mapping()
        except common.FetchError as err:
            results.append(
                common.Result(status='FAIL', name='mapping', evidence=str(err))
            )
        else:
            results.extend(compare(pyproject, recipe, mapping))
    if not args.dry_run and text != recipe.text:
        paths[0].write_text(text)
        results.append(
            common.Result(
                status='PASS', name='written', evidence=str(paths[0])
            )
        )
    report(results)
    return exit_status(results)


def command_verify(args: argparse.Namespace) -> int:
    """Poll anaconda.org until the version is on conda-forge."""
    pyproject: dict[str, object] = read_pyproject(pathlib.Path(args.pyproject))
    package: str = args.package or project_name(pyproject)
    conda_name: str
    try:
        conda_name = args.conda_name or conda_name_for(package, load_mapping())
    except common.FetchError as err:
        report(
            [common.Result(status='FAIL', name='mapping', evidence=str(err))]
        )
        return 1

    def probe() -> common.Result:
        return verify_once(conda_name, args.version)

    deadline: float = time.monotonic() + args.wait
    result: common.Result = probe()
    while result.status == 'FAIL' and time.monotonic() < deadline:
        time.sleep(30)
        result = probe()
    report([result])
    return exit_status([result])


def verify_once(conda_name: str, version: str) -> common.Result:
    """One look at anaconda.org."""
    try:
        listed: list[str] | None = conda_versions(conda_name)
    except common.FetchError as err:
        return common.Result(
            status='FAIL', name='conda-forge', evidence=str(err)
        )
    if listed is None:
        return common.Result(
            status='SKIP',
            name='conda-forge',
            evidence=(f'anaconda.org has no package conda-forge/{conda_name}'),
        )
    if version in listed:
        return common.Result(
            status='PASS',
            name='conda-forge',
            evidence=(
                f'https://anaconda.org/conda-forge/{conda_name} '
                f'lists {version}'
            ),
        )
    return common.Result(
        status='FAIL',
        name='conda-forge',
        evidence=(
            f'conda-forge/{conda_name} latest is '
            f'{listed[-1] if listed else "nothing"}, no {version}'
        ),
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the command line."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog='ppt conda-forge', description=(__doc__ or '').split('\n\n')[0]
    )
    commands: argparse._SubParsersAction[argparse.ArgumentParser] = (
        parser.add_subparsers(dest='command', required=True)
    )
    for name, handler in (
        ('status', command_status),
        ('prepare', command_prepare),
        ('verify', command_verify),
    ):
        sub: argparse.ArgumentParser = commands.add_parser(name)
        sub.set_defaults(handler=handler)
        sub.add_argument(
            '--version', required=True, help='released version, e.g. 4.1.0'
        )
        sub.add_argument(
            '--package', help='PyPI name, read from pyproject.toml by default'
        )
        sub.add_argument(
            '--pyproject',
            default='pyproject.toml',
            help='pyproject.toml of the released package',
        )
        if name == 'status':
            sub.add_argument(
                '--feedstock',
                help='OWNER/REPO when the feedstock is not <package>-feedstock',  # noqa: E501
            )
            sub.add_argument('--json', help='also write the results here')
        if name == 'prepare':
            sub.add_argument(
                '--dir', required=True, help='feedstock checkout to edit'
            )
            sub.add_argument(
                '--dry-run',
                action='store_true',
                help='report the edits without writing',
            )
        if name == 'verify':
            sub.add_argument(
                '--conda-name',
                help='conda-forge package name when it differs from PyPI',
            )
            sub.add_argument(
                '--wait',
                type=int,
                default=0,
                help='seconds to keep polling anaconda.org',
            )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """Dispatch to the subcommand."""
    args: argparse.Namespace = parse_args(argv)
    return int(args.handler(args))


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
