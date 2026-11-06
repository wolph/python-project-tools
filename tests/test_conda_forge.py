"""Tests for the pure parts of conda_forge.py: recipe parsing, edits, comparisons.

Run from the repository root::

    uv run pytest tests/test_conda_forge.py

Nothing here touches the network or gh. The two recipe samples follow the
shapes conda-forge actually uses: a jinja ``meta.yaml`` and a v1
``recipe.yaml``.
"""  # noqa: E501

from __future__ import annotations

import pytest

from python_project_tools import conda_forge

META: str = """{% set name = "spam-utils" %}
{% set version = "0.3.1" %}
{% set sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" %}

package:
  name: {{ name|lower }}
  version: {{ version }}

source:
  url: https://files.pythonhosted.org/packages/source/{{ name[0] }}/{{ name }}/{{ name }}-{{ version }}.tar.gz
  sha256: {{ sha256 }}

build:
  noarch: python
  number: 2
  script: {{ PYTHON }} -m pip install . --no-deps --no-build-isolation -vv
  entry_points:
    - spam = spamutils.main:main

requirements:
  host:
    - pip
    - python {{ python_min }}
    - setuptools
  run:
    - python >={{ python_min }}
    - numpy >=1.24
    - typing_extensions >=4.1  # [py<311]

test:
  imports:
    - spamutils

about:
  home: https://github.com/wolph/spam-utils
  license: BSD-3-Clause

extra:
  recipe-maintainers:
    - wolph
    - eggs
"""  # noqa: E501

V1: str = """schema_version: 1

context:
  name: spamutils
  version: "0.3.1"

package:
  name: ${{ name|lower }}
  version: ${{ version }}

source:
  url: https://pypi.org/packages/source/${{ name[0] }}/spam-utils/spam_utils-${{ version }}.tar.gz
  sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa

build:
  number: 1
  noarch: python
  script: ${{ PYTHON }} -m pip install . -vv

requirements:
  host:
    - pip
    - python ${{ python_min }}.*
    - uv-build
  run:
    - python >=${{ python_min }}

extra:
  recipe-maintainers:
    - wolph
"""  # noqa: E501

NEW_SHA: str = 'b' * 64
SDIST: conda_forge.Sdist = conda_forge.Sdist(
    'spam_utils-0.4.0.tar.gz',
    'https://files.pythonhosted.org/x/spam_utils-0.4.0.tar.gz',
    NEW_SHA,
)
PYPROJECT: dict[str, object] = {
    'build-system': {
        'build-backend': 'uv_build',
        'requires': ['uv_build>=0.11,<0.13'],
    },
    'project': {
        'name': 'spam-utils',
        'requires-python': '>=3.10',
        'dependencies': [
            'numpy>=1.24',
            'python-utils>=3.4',
            'typing_extensions>=4.1; python_version < "3.11"',
        ],
        'optional-dependencies': {
            'win32': ['pywin32>=226; platform_system == "Windows"']
        },
        'scripts': {
            'spam': 'spamutils.main:main',
            'eggs': 'spamutils.main:eggs',
        },
    },
}


def test_parse_meta() -> None:
    recipe: conda_forge.Recipe = conda_forge.parse_recipe(
        'recipe/meta.yaml', META
    )
    assert recipe.style == 'meta'
    assert (recipe.name, recipe.version, recipe.build_number) == (
        'spam-utils',
        '0.3.1',
        2,
    )
    assert recipe.sha256 == 'a' * 64
    assert recipe.url.endswith('{{ name }}-{{ version }}.tar.gz')
    assert [item.name for item in recipe.host] == [
        'pip',
        'python',
        'setuptools',
    ]
    assert [(item.name, item.spec) for item in recipe.run] == [
        ('python', '>={{python_min}}'),
        ('numpy', '>=1.24'),
        ('typing_extensions', '>=4.1'),
    ]
    assert recipe.entry_points == ['spam = spamutils.main:main']
    assert recipe.maintainers == ['wolph', 'eggs']


def test_parse_v1() -> None:
    recipe: conda_forge.Recipe = conda_forge.parse_recipe(
        'recipe/recipe.yaml', V1
    )
    assert recipe.style == 'v1'
    assert (recipe.name, recipe.version, recipe.build_number) == (
        'spamutils',
        '0.3.1',
        1,
    )
    assert recipe.sha256 == 'a' * 64
    assert [item.name for item in recipe.host] == ['pip', 'python', 'uv-build']
    assert recipe.entry_points == []
    assert recipe.maintainers == ['wolph']


def test_render_handles_the_feedstock_expressions() -> None:
    assert (
        conda_forge.render(
            "{{ name.replace('-', '_') }}-{{ version }}.tar.gz",
            'spam-utils',
            '0.4.0',
        )
        == 'spam_utils-0.4.0.tar.gz'
    )
    assert (
        conda_forge.render('${{ name[0] }}/${{ name|lower }}', 'Spam', '1')
        == 'S/spam'
    )
    with pytest.raises(conda_forge.TemplateError):
        conda_forge.render('{{ name|upper }}', 'spam', '1')


def test_filename_template_round_trips() -> None:
    template: str = conda_forge.filename_template(
        'spam_utils-0.4.0.tar.gz', 'spam-utils', '0.4.0', 'meta'
    )
    assert template == "{{ name.replace('-', '_') }}-{{ version }}.tar.gz"
    assert (
        conda_forge.render(template, 'spam-utils', '0.4.0')
        == 'spam_utils-0.4.0.tar.gz'
    )
    assert (
        conda_forge.filename_template(
            'spam-0.4.0.tar.gz', 'spam', '0.4.0', 'v1'
        )
        == '${{ name }}-${{ version }}.tar.gz'
    )
    assert (
        conda_forge.filename_template(
            'mt_940-5.1.1.tar.gz', 'mt940', '5.1.1', 'v1'
        )
        == 'mt_940-${{ version }}.tar.gz'
    )


def test_prepare_meta_edits_everything_the_bot_misses() -> None:
    recipe: conda_forge.Recipe = conda_forge.parse_recipe(
        'recipe/meta.yaml', META
    )
    text: str
    results: list[conda_forge.Result]
    text, results = conda_forge.prepare_text(recipe, '0.4.0', SDIST, PYPROJECT)
    assert '{% set version = "0.4.0" %}' in text
    assert f'{{% set sha256 = "{NEW_SHA}" %}}' in text
    assert '  number: 0\n' in text
    assert "{{ name.replace('-', '_') }}-{{ version }}.tar.gz" in text
    assert '    - pip\n    - uv-build >=0.11,<0.13\n' in text
    assert {result.name: result.status for result in results} == {
        'version': 'EDIT',
        'build-number': 'EDIT',
        'sha256': 'EDIT',
        'url': 'EDIT',
        'backend': 'EDIT',
    }
    # The edit leaves everything else byte for byte alone.
    assert text.count('\n') == META.count('\n') + 1


def test_prepare_v1_edits_version_sha_and_number() -> None:
    recipe: conda_forge.Recipe = conda_forge.parse_recipe(
        'recipe/recipe.yaml', V1
    )
    sdist: conda_forge.Sdist = conda_forge.Sdist(
        'spam_utils-0.4.0.tar.gz', 'u', NEW_SHA
    )
    text: str
    results: list[conda_forge.Result]
    text, results = conda_forge.prepare_text(recipe, '0.4.0', sdist, {})
    assert '  version: "0.4.0"\n' in text
    assert f'  sha256: {NEW_SHA}\n' in text
    assert '  number: 0\n' in text
    assert [result.status for result in results] == [
        'EDIT',
        'EDIT',
        'EDIT',
        'INFO',
    ]


def test_prepare_is_idempotent_on_a_current_recipe() -> None:
    recipe: conda_forge.Recipe = conda_forge.parse_recipe(
        'recipe/meta.yaml', META
    )
    sdist: conda_forge.Sdist = conda_forge.Sdist(
        'spam-utils-0.3.1.tar.gz', 'u', 'a' * 64
    )
    text: str
    results: list[conda_forge.Result]
    text, results = conda_forge.prepare_text(recipe, '0.3.1', sdist, {})
    assert text == META
    assert all(result.status == 'INFO' for result in results)


def test_backend_requirement_maps_pypi_backends_to_conda_names() -> None:
    assert conda_forge.backend_requirement(
        PYPROJECT
    ) == conda_forge.Requirement('uv-build', '>=0.11,<0.13')
    hatch: dict[str, object] = {
        'build-system': {
            'build-backend': 'hatchling.build',
            'requires': ['hatchling'],
        }
    }
    assert conda_forge.backend_requirement(hatch) == conda_forge.Requirement(
        'hatchling', ''
    )
    assert (
        conda_forge.backend_requirement(
            {'build-system': {'build-backend': 'custom.backend'}}
        )
        is None
    )


def test_add_backend_reports_an_existing_pin_instead_of_duplicating() -> None:
    recipe: conda_forge.Recipe = conda_forge.parse_recipe(
        'recipe/recipe.yaml', V1
    )
    text: str
    results: list[conda_forge.Result]
    text, results = conda_forge.add_backend(
        V1, recipe, conda_forge.Requirement('uv-build', '>=0.11,<0.13')
    )
    assert text == V1
    assert results[0].status == 'WARN'
    assert 'pyproject says >=0.11,<0.13' in results[0].evidence


def test_compare_finds_the_drift_the_bot_cannot_see() -> None:
    recipe: conda_forge.Recipe = conda_forge.parse_recipe(
        'recipe/meta.yaml', META
    )
    mapping: dict[str, dict[str, str]] = {
        'typing-extensions': {'conda_name': 'typing_extensions'}
    }
    results: list[conda_forge.Result] = conda_forge.compare(
        PYPROJECT, recipe, mapping
    )
    warnings: dict[str, list[str]] = {}
    for result in results:
        if result.status == 'WARN':
            warnings.setdefault(result.name, []).append(result.evidence)
    assert any(
        'recipe run lacks python-utils' in text for text in warnings['run']
    )
    assert any(
        'recipe run has typing_extensions' in text for text in warnings['run']
    )
    assert any('conditional dependency' in text for text in warnings['run'])
    assert warnings['entry-points'] == [
        'pyproject defines eggs=spamutils.main:eggs, recipe does not list it'
    ]
    assert warnings['host'] == [
        'host still lists setuptools while the backend is uv-build'
    ]
    assert (
        'python' not in warnings
    )  # python_min placeholder is conda-forge's business


def test_compare_run_names_the_extra_a_leftover_came_from() -> None:
    recipe: conda_forge.Recipe = conda_forge.parse_recipe(
        'recipe/meta.yaml', META.replace('numpy >=1.24', 'pywin32  # [win]')
    )
    results: list[conda_forge.Result] = conda_forge.compare_run(
        PYPROJECT, recipe, {}
    )
    assert any(
        result.evidence == 'recipe run has pywin32, only in extra win32'
        for result in results
    )


def test_compare_python_flags_a_literal_floor_that_differs() -> None:
    recipe: conda_forge.Recipe = conda_forge.parse_recipe(
        'recipe/meta.yaml',
        META.replace('python >={{ python_min }}', 'python >=3.8'),
    )
    results: list[conda_forge.Result] = conda_forge.compare_python(
        PYPROJECT, recipe
    )
    assert results == [
        conda_forge.Result(
            'WARN',
            'python',
            'recipe run has python >=3.8, pyproject requires-python >=3.10',
        )
    ]


def test_version_key_orders_numerically() -> None:
    assert conda_forge.version_key('4.10.0') > conda_forge.version_key('4.9.1')
    assert conda_forge.version_key('4.1.0') < conda_forge.version_key(
        '4.1.0.post1'
    )


def test_stale_prs_are_the_ones_main_overtook() -> None:
    prs: list[conda_forge.BotPR] = [
        conda_forge.BotPR(55, 'spam v4.0.1', '4.0.1', 'h1', 'u', '2026-09-07'),
        conda_forge.BotPR(58, 'spam v4.1.0', '4.1.0', 'h2', 'u', '2026-10-16'),
        conda_forge.BotPR(59, 'spam v4.2.0', '4.2.0', 'h3', 'u', '2026-10-18'),
    ]
    assert [pr.number for pr in conda_forge.stale_prs(prs, '4.1.0')] == [
        55,
        58,
    ]


def test_bot_title_regex_accepts_both_title_forms() -> None:
    match = conda_forge.BOT_TITLE.match('[bot-automerge] portalocker v4.4.1')
    assert match is not None and match.group(2) == '4.4.1'
    assert conda_forge.BOT_TITLE.match('numpy-stl v4.1.0').group(2) == '4.1.0'  # ty: ignore[unresolved-attribute]
    assert conda_forge.BOT_TITLE.match('Rebuild for python 3.15') is None


def test_conda_forge_is_registered() -> None:
    from python_project_tools import cli

    assert cli.COMMANDS['conda-forge'] is conda_forge.main
