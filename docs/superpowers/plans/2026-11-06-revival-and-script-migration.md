# python-project-tools revival and release script migration (plan 1 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the archived 2012 repository into a modern, tested package with
a `ppt` CLI that carries the release-python-package skill's scripts as
subcommands, and point the skill at it.

**Architecture:** Flat layout, `python_project_tools/` at the repository root,
built with uv_build. `cli.py` holds one argparse dispatcher that maps a
subcommand name to a module's existing `main(argv: list[str]) -> int`, so the
moved scripts keep their parsers unchanged. Each moved script becomes a module
with its tests beside it in `tests/`. The skill text in `~/workspace/ai`
changes from script paths to `ppt` commands and loses its copy of the code.

**Tech Stack:** Python >=3.10, uv, uv_build, argparse, packaging, pytest,
ruff, ty, tox-uv, lefthook, GitHub Actions (ci, autofix.ci, CodeQL, stale).

**Spec:** `docs/superpowers/specs/2026-11-06-signed-releases-design.md`,
sections "1. python-project-tools revival", "2. CLI surface" (the four moved
commands only) and the calls on the skill text and the editable install.
Plans 2 (backfill tooling) and 3 (future-release pipeline) build on this one.

## Global Constraints

- `requires-python = '>=3.10'`, the same floor as portalocker.
- `python_project_tools/api_manifest.py` stays stdlib-only and runs on
  Python 3.9 and later: `check_compat` executes it as a file under other
  interpreters with `-I`, where the package is not installed.
- Runtime dependencies: `packaging` only. Nothing else without a reason in
  the commit message.
- Console script name `ppt`, distribution `python-project-tools`, module
  `python_project_tools`. Not published to PyPI.
- Style: `ruff.toml` copied from portalocker (79 columns, single quotes,
  google docstrings), `ty` clean, explicit type annotations on every
  variable and signature, tests under `tests/`.
- Prose (README, CHANGELOG, docstrings, commit messages): ASCII punctuation,
  no semicolons, British spelling.
- Git: default branch `master`, work on a feature branch in a worktree,
  signed commits. Nothing is pushed until the repository is dearchived
  (prerequisite, Rick's step).
- The 2012 files are removed in one commit, not edited. History keeps them.

## Review Focus

1. `ppt nonsense` and bare `ppt` exit 2 with usage on stderr, never a
   traceback. Test in Task 1.
2. `check_compat` finds `api_manifest.py` by package path after the move and
   still runs it under `python -I` from another interpreter. Test in Task 2.
3. `python -m python_project_tools --version` prints the version from
   installed metadata, so the version has one source. Test in Task 1.
4. `conda_forge` tests used `SCRIPTS`-relative paths. After the move they
   resolve through the package and pass from any working directory. Test in
   Task 4.
5. `ppt verify-release` run inside another package's checkout still reads
   that checkout's `pyproject.toml` for the package name, as the skill relies
   on. Test in Task 3.

---

### Task 1: Clear the 2012 tree and scaffold the package with a `ppt` dispatcher

**Files:**
- Delete: `.project`, `.pydevproject`, `.travis.yml`, `MANIFEST.in`,
  `README.rst`, `requirements.txt`, `setup.cfg`, `setup.py`, `files/`,
  `templates/`, `eggs/`, `python_project_tools/` (old contents), every `*.pyc`
  and `*.swp`, `docs/` except `docs/superpowers/`
- Create: `pyproject.toml`, `ruff.toml`, `tox.toml`, `.gitignore`, `LICENSE`,
  `README.md`, `CHANGELOG.rst`, `lefthook.yml`,
  `python_project_tools/__init__.py`, `python_project_tools/__about__.py`,
  `python_project_tools/__main__.py`, `python_project_tools/cli.py`,
  `python_project_tools/py.typed`
- Test: `tests/__init__.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `python_project_tools.cli.COMMANDS: dict[str, Callable[[list[str]], int]]`
  and `python_project_tools.cli.main(argv: list[str] | None = None) -> int`.
  Later tasks add one entry each to `COMMANDS`. A command receives the
  arguments after its name and returns the exit status.
- Produces: `python_project_tools.__about__.__version__: str`, read with
  `importlib.metadata.version('python-project-tools')`, re-exported from
  `__init__.py`.

- [ ] **Step 1: Remove the 2012 tree**

```bash
cd <worktree>
git rm -rq .project .pydevproject .travis.yml MANIFEST.in README.rst requirements.txt setup.cfg setup.py files templates python_project_tools
git ls-files docs | grep -v '^docs/superpowers/' | xargs git rm -q
rm -rf eggs files templates python_project_tools .ropeproject .fseventsd
git ls-files # expect .gitignore and docs/superpowers/** only
git commit -S -m 'chore: remove the 2012 code, this repository starts over

The old scripts, templates and setup.py stay in history. The next commits
build a modern package in their place.'
```

- [ ] **Step 2: Write the failing CLI tests**

`tests/test_cli.py`:

```python
import subprocess
import sys

import pytest

from python_project_tools import __version__, cli


def test_version_matches_metadata() -> None:
    import importlib.metadata

    assert __version__ == importlib.metadata.version('python-project-tools')


def test_module_entry_prints_version() -> None:
    done: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, '-m', 'python_project_tools', '--version'],
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 0
    assert done.stdout.strip() == f'ppt {__version__}'


@pytest.mark.parametrize('argv', [[], ['nonsense']])
def test_unknown_or_missing_command_exits_2(
    argv: list[str], capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    assert 'usage: ppt' in capsys.readouterr().err


def test_registered_command_receives_remaining_argv() -> None:
    seen: list[list[str]] = []

    def fake(argv: list[str]) -> int:
        seen.append(argv)
        return 7

    cli.COMMANDS['fake'] = fake
    try:
        assert cli.main(['fake', '--x', '1']) == 7
    finally:
        del cli.COMMANDS['fake']
    assert seen == [['--x', '1']]
```

- [ ] **Step 3: Write `pyproject.toml`**

Copy portalocker's shape. Values that differ:

```toml
[project]
name = 'python-project-tools'
version = '2.0.0'
description = 'Release, signing and verification tooling for Python packages'
readme = 'README.md'
requires-python = '>=3.10'
license = 'BSD-3-Clause'
license-files = ['LICENSE']
dependencies = ['packaging>=23']

[project.scripts]
ppt = 'python_project_tools.cli:main'

[project.optional-dependencies]
tests = ['pytest>=8', 'pytest-cov>=5']

[dependency-groups]
dev = ['python-project-tools[tests]', 'ruff', 'ty', 'tox-uv', 'tox>=4.21']

[tool.uv.build-backend]
module-name = 'python_project_tools'
module-root = ''

[tool.pytest.ini_options]
addopts = ['--strict-config', '--strict-markers', '-ra', '-p', 'no:cacheprovider']
testpaths = ['tests']
xfail_strict = true
filterwarnings = ['error']

[tool.ty.environment]
python-version = '3.10'

[tool.ty.src]
include = ['python_project_tools', 'tests']
```

Classifiers: `Development Status :: 4 - Beta`, `Environment :: Console`,
`Intended Audience :: Developers`, the five Python 3.x classifiers,
`Topic :: Software Development :: Build Tools`, `Typing :: Typed`. URLs:
homepage and repository `https://github.com/WoLpH/python-project-tools/`.
No `[tool.coverage]` yet, coverage comes with plan 2's larger surface.
Version 2.0.0 because PyPI history and the 2012 README already used 1.x
naming for the old tool.

- [ ] **Step 4: Copy `ruff.toml`, `lefthook.yml` and `.gitignore` from `~/workspace/portalocker`, write `tox.toml`**

`ruff.toml`: identical except the `per-file-ignores` keys become
`'tests/*' = ['D100', 'D101', 'D102', 'D103', 'D104', 'D105', 'D107', 'INP001']`
and the `portalocker_tests/tests.py` line goes. `.gitignore`: portalocker's
minus `locked_file`, `*.lock_file`, `gpgit/`, with `docs/superpowers/` not
ignored. `tox.toml` keeps only these envs from portalocker's file: `py310`
to `py314`, `ruff`, `ruff-fix`, `ty`, `zizmor`, with `extras = ['tests']` and
`commands = [['pytest', { replace = 'posargs', extend = true }]]` in
`env_run_base`.

- [ ] **Step 5: Write `LICENSE`, `README.md`, `CHANGELOG.rst`**

`LICENSE`: BSD-3-Clause, copyright Rick van Hattem, copy
`~/workspace/portalocker/LICENSE` and keep the year range from the old
repository's first commit (2012) to 2026. `README.md`: title, one paragraph
on what `ppt` is for (release tooling behind the release-python-package
skill), an "Install" section with exactly
`uv tool install --editable ~/workspace/python-project-tools`, and a
"Commands" section listing `check-compat`, `verify-release`, `conda-forge`
with one line each, to be extended by plan 2. `CHANGELOG.rst` in
portalocker's format with a `2.0.0:` section: "Fresh start. Modern package,
`ppt` CLI, release scripts moved here from the release-python-package
skill."

- [ ] **Step 6: Implement the package**

`python_project_tools/__about__.py`: `__version__: str` from
`importlib.metadata.version('python-project-tools')`.
`python_project_tools/__init__.py`: re-export `__version__`.
`python_project_tools/cli.py`:

```python
COMMANDS: dict[str, Callable[[list[str]], int]] = {}

def main(argv: list[str] | None = None) -> int: ...
```

`main` builds `argparse.ArgumentParser(prog='ppt')` with `--version`
(`action='version'`, `version=f'ppt {__version__}'`) and one positional
`command` with `choices=sorted(COMMANDS)`, parses with `parse_known_args`,
and returns `COMMANDS[command](rest)`. A missing or unknown command is
argparse's own error, which exits 2 with usage on stderr.
`python_project_tools/__main__.py`: `raise SystemExit(main())`.
Empty `py.typed`. Empty `tests/__init__.py`.

- [ ] **Step 7: Run the tests, ruff and ty**

Run: `uv sync --group dev && uv run pytest -v && uv run ruff check && uv run ruff format --check && uv run ty check`
Expected: 5 passed, ruff and ty clean.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -S -m 'feat: modern package skeleton with the ppt dispatcher'
```

### Task 2: Move `check_compat`, `compat_rules` and `api_manifest`

**Files:**
- Create: `python_project_tools/check_compat.py`,
  `python_project_tools/compat_rules.py`,
  `python_project_tools/api_manifest.py` (moved with `git mv` after
  copying from `~/workspace/ai/skills/release-python-package/scripts/`, the
  ai repository is edited in Task 6)
- Modify: `python_project_tools/cli.py` (register `check-compat`)
- Test: `tests/test_compat.py` (moved from the skill's `tests/`)

**Interfaces:**
- Consumes: `cli.COMMANDS`.
- Produces: `python_project_tools.check_compat.main(argv: list[str]) -> int`,
  `python_project_tools.api_manifest.__file__` as the path `check_compat`
  runs under other interpreters.

- [ ] **Step 1: Copy the three scripts and the test file into place**

```bash
S=~/workspace/ai/skills/release-python-package
cp $S/scripts/{check_compat,compat_rules,api_manifest}.py python_project_tools/
cp $S/tests/test_compat.py tests/test_compat.py
```

- [ ] **Step 2: Adapt the test imports and add the dispatcher test**

In `tests/test_compat.py`: delete the `SCRIPTS` constant and the
`sys.path.insert`, replace `import compat_rules` with
`from python_project_tools import compat_rules`, and in the subprocess test
replace `str(SCRIPTS / 'api_manifest.py')` with
`python_project_tools.api_manifest.__file__`. Append:

```python
def test_check_compat_is_registered() -> None:
    from python_project_tools import check_compat, cli

    assert cli.COMMANDS['check-compat'] is check_compat.main
```

- [ ] **Step 3: Run the tests to see the import and registration failures**

Run: `uv run pytest tests/test_compat.py -v`
Expected: FAIL on `check_compat` importing `compat_rules` from `sys.path`
(remove the `sys.path.insert` block, lines 43 to 45, and import
`from python_project_tools import compat_rules` and
`from python_project_tools.compat_rules import Finding`) and on the missing
registration.

- [ ] **Step 4: Fix the module imports and the `api_manifest` path, register the command**

In `check_compat.py` replace `HERE / 'api_manifest.py'` with
`pathlib.Path(api_manifest.__file__)` after
`from python_project_tools import api_manifest`. Remove `HERE` if nothing
else uses it. In `cli.py` add `from python_project_tools import check_compat`
and `COMMANDS['check-compat'] = check_compat.main`. Keep `api_manifest.py`
free of package imports so it still runs as a file on 3.9.

- [ ] **Step 5: Run tests, ruff and ty**

Run: `uv run pytest -v && uv run ruff check --fix && uv run ruff format && uv run ty check`
Expected: 14 compat tests plus Task 1's pass. Fix ruff findings in the moved
code when they are mechanical (quotes, imports). Anything else gets a
`noqa` with the rule name and no behaviour change in this task.

- [ ] **Step 6: Commit**

```bash
git add python_project_tools tests
git commit -S -m 'feat: check-compat command, moved from the release skill'
```

### Task 3: Move `verify_release`

**Files:**
- Create: `python_project_tools/verify_release.py`
- Modify: `python_project_tools/cli.py`
- Test: `tests/test_verify_release.py`

**Interfaces:**
- Produces: `python_project_tools.verify_release.main(argv: list[str]) -> int`.
  Plan 2 extends this module with the signature checks.

- [ ] **Step 1: Write the failing tests**

```python
import pathlib

import pytest

from python_project_tools import cli, verify_release


def test_verify_release_is_registered() -> None:
    assert cli.COMMANDS['verify-release'] is verify_release.main


def test_help_lists_the_skip_groups(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        verify_release.main(['--help'])
    assert exc.value.code == 0
    assert 'pypi,github,stable,docs,install,conda' in capsys.readouterr().out


def test_package_name_defaults_to_cwd_pyproject(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / 'pyproject.toml').write_text("[project]\nname = 'spam-eggs'\n")
    monkeypatch.chdir(tmp_path)
    assert verify_release.read_package_name() == 'spam-eggs'
```

The third test names the function the script uses to read the package name
from `pyproject.toml`. If the script does it inline in `main`, extract
`read_package_name(root: pathlib.Path = pathlib.Path('.')) -> str` in Step 3
without changing what `main` does.

- [ ] **Step 2: Copy the script, run the tests to see them fail**

```bash
cp ~/workspace/ai/skills/release-python-package/scripts/verify_release.py python_project_tools/
uv run pytest tests/test_verify_release.py -v
```

Expected: FAIL on registration and on `read_package_name`.

- [ ] **Step 3: Register the command and expose `read_package_name`**

`cli.py`: `COMMANDS['verify-release'] = verify_release.main`.

- [ ] **Step 4: Run tests, ruff and ty**

Run: `uv run pytest -v && uv run ruff check --fix && uv run ruff format && uv run ty check`
Expected: all pass, clean.

- [ ] **Step 5: Commit**

```bash
git add python_project_tools tests
git commit -S -m 'feat: verify-release command, moved from the release skill'
```

### Task 4: Move `conda_forge`

**Files:**
- Create: `python_project_tools/conda_forge.py`
- Modify: `python_project_tools/cli.py`
- Test: `tests/test_conda_forge.py` (moved)

**Interfaces:**
- Produces: `python_project_tools.conda_forge.main(argv: list[str]) -> int`
  with its existing `status`, `prepare`, `verify` subcommands.

- [ ] **Step 1: Copy the script and tests, adapt the test imports**

```bash
S=~/workspace/ai/skills/release-python-package
cp $S/scripts/conda_forge.py python_project_tools/
cp $S/tests/test_conda_forge.py tests/
```

In the test: delete `SCRIPTS` and the `sys.path.insert`, use
`from python_project_tools import conda_forge`. Any fixture path built from
`SCRIPTS` becomes `pathlib.Path(__file__).parent / 'fixtures' / ...` and
the fixture files move along. Append:

```python
def test_conda_forge_is_registered() -> None:
    from python_project_tools import cli

    assert cli.COMMANDS['conda-forge'] is conda_forge.main
```

- [ ] **Step 2: Run the tests to see the registration failure**

Run: `cd /tmp && uv run --project <worktree> pytest <worktree>/tests/test_conda_forge.py -v`
Expected: 15 pass, the registration test fails. Running from `/tmp` proves
Review Focus 4: no path depends on the working directory.

- [ ] **Step 3: Register the command**

`cli.py`: `COMMANDS['conda-forge'] = conda_forge.main`.

- [ ] **Step 4: Run tests, ruff and ty**

Run: `uv run pytest -v && uv run ruff check --fix && uv run ruff format && uv run ty check`
Expected: all pass, clean. conda_forge.py uses double quotes, ruff format
rewrites them. That is the one wholesale change allowed in the move.

- [ ] **Step 5: Commit**

```bash
git add python_project_tools tests
git commit -S -m 'feat: conda-forge command, moved from the release skill'
```

### Task 5: CI, autofix, CodeQL, stale

**Files:**
- Create: `.github/workflows/ci.yml`, `.github/workflows/autofix.yml`,
  `.github/workflows/codeql.yml`, `.github/workflows/stale.yml`

- [ ] **Step 1: Copy the four workflows from `~/workspace/portalocker/.github/workflows/`**

`ci.yml`: keep the `checks` matrix with `env: [ty]` only, the `ruff` job,
the `zizmor` job and the test matrix job, drop every job that names an env
not in this repository's `tox.toml` (mypy, basedpyright, pyrefly, codespell,
repo-review, docs, distribution, redis, win32). Test matrix: ubuntu, macos,
windows, Python 3.10 to 3.14. `autofix.yml`, `codeql.yml`, `stale.yml`:
unchanged.

- [ ] **Step 2: Lint the workflows**

Run: `uv run tox -e zizmor`
Expected: no findings.

- [ ] **Step 3: Run the whole tox matrix locally once**

Run: `uv run tox -e py310,py314,ruff,ty,zizmor`
Expected: every env green.

- [ ] **Step 4: Commit**

```bash
git add .github
git commit -S -m 'ci: tests, ruff, ty, zizmor, autofix, CodeQL and stale'
```

### Task 6: Point the release skill at `ppt` and delete its copy of the code

**Files:**
- Modify: `~/workspace/ai/skills/release-python-package/SKILL.md:97,252,289,325,378`
- Delete: `~/workspace/ai/skills/release-python-package/scripts/`,
  `~/workspace/ai/skills/release-python-package/tests/`
- Check: `~/workspace/ai/skills/release-python-package/evals/` for
  references to `scripts/`, fix them the same way or note why not.

- [ ] **Step 1: Install the tool and prove the commands exist**

Run: `uv tool install --editable <worktree> && ppt --version && ppt check-compat --help >/dev/null && ppt verify-release --help >/dev/null && ppt conda-forge --help >/dev/null && echo OK`
Expected: `ppt 2.0.0` then `OK`.

- [ ] **Step 2: Edit SKILL.md**

Replace `python -I <skill-dir>/scripts/check_compat.py` with
`ppt check-compat`, `python3 -I <skill-dir>/scripts/verify_release.py` with
`ppt verify-release`, `python3 -I <skill-dir>/scripts/conda_forge.py` with
`ppt conda-forge`, keeping every argument that follows. In "Phase 0" add
one line: "The commands below come from `ppt`, installed once with
`uv tool install --editable ~/workspace/python-project-tools`. If `ppt` is
missing, stop and install it." Grep the file for `scripts/` afterwards and
expect no hits.

- [ ] **Step 3: Remove the scripts and tests from the skill**

```bash
cd ~/workspace/ai
git rm -rq skills/release-python-package/scripts skills/release-python-package/tests
grep -rn 'scripts/' skills/release-python-package/ || echo 'no references left'
```

Expected: `no references left`. If the evals reference the scripts, update
them to `ppt` in the same commit.

- [ ] **Step 4: Commit in the ai repository, staging only the skill**

```bash
cd ~/workspace/ai
git add skills/release-python-package
git commit -S -m 'release-python-package: call ppt instead of bundled scripts

The scripts and their tests now live in python-project-tools as the
check-compat, verify-release and conda-forge commands.'
```

The working tree has unrelated changes under `skills/author-voice-pass/`.
They stay unstaged.

### Task 7: Finish the branch

- [ ] **Step 1: Full verification from a clean state**

Run: `cd <worktree> && git status --porcelain && uv run tox -e py310,py314,ruff,ty,zizmor && ppt --version`
Expected: empty status, all green, `ppt 2.0.0`.

- [ ] **Step 2: Report to Rick and wait for the dearchive**

The push, the pull request against `master` and the CI run need
`gh repo unarchive WoLpH/python-project-tools`. Report the branch name, the
commit list and the local tox result, and stop. After the dearchive: fetch,
rebase on `origin/master`, rerun Step 1, push, open the PR, confirm
`gh pr view --json mergeable`, watch CI to green, merge. Follow the git
rules in CLAUDE.md for the watch.
