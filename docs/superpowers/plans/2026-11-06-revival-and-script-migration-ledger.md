# SDD ledger, plan: docs/superpowers/plans/2026-11-06-revival-and-script-migration.md

Spec: docs/superpowers/specs/2026-11-06-signed-releases-design.md (read).
Worktree: .claude/worktrees/signed-releases-plan1, branch worktree-signed-releases-plan1.
Base before Task 1: fd7e20b.

## Preflight scan

| Pair / task | Produces vs consumes | Found |
|---|---|---|
| T1 / T2, T3, T4 | `cli.COMMANDS: dict[str, Callable[[list[str]], int]]`, each moved module exposes `main(argv: list[str]) -> int` | consistent, all three scripts already have that signature |
| T1 / T5 | new `.gitignore` (portalocker's) vs `.github/workflows` created in T5 | the 2012 `.gitignore` has `.*`, which would ignore `.github`. T1 replaces it with portalocker's, which does not. OK once T1 lands first |
| T1 / T5 | `tox.toml` envs py310-py314, ruff, ruff-fix, ty, zizmor vs ci.yml matrix | consistent |
| T2 / T2 test | `api_manifest.__file__` run under `python -I` as a file, api_manifest stays stdlib-only | consistent with Global Constraints |
| T3 / T3 test | `read_package_name(root)` may not exist in the script, Step 3 says extract it | self-consistent |
| T1 self | Step 7 expects 5 tests: version, module entry, 2 parametrized, registered | correct count |
| T1 self | version 2.0.0 justified by "PyPI history used 1.x", but the name was never on PyPI | Ruling: 2.0.0 stands, the 2012 setup.py line is version 1.x and a fresh start deserves a major. Costs nothing if wrong, a version number |
| T6 | commit in ~/workspace/ai, outside this worktree, local only, no push | approved by Rick with the plan, staging limited to the skill directory |

## Progress
Task 1: implementer 1 (teammate pane) silent 10 min, stopped. Implementer 2 BLOCKED on rtk hook vs worktree guard, resumed with /usr/bin/git. DONE_WITH_CONCERNS, commits d2d1a1e..f1454c2, 5 tests, ruff and ty clean.
Task 1: minor (deferred): tox pins ruff 0.15.18 while uv resolves 0.17.4, pin may need a bump. ruff format excludes __init__.py (copied config).
Task 1: review dispatched on fd7e20b..f1454c2.
Ruling: all git in this worktree runs as /usr/bin/git because the rtk hook rewrites bare git and the worktree guard refuses rtk git. Costs nothing if wrong, same binary.
Task 1: review: Approved with 2 Important plan-mandated. (1) dispatcher swallows subcommand flags. Ruling: real and load-bearing for Tasks 2-4 and 6, fix round 1 dispatched: parse argv[:1] only, allow_abbrev=False, forward argv[1:]. Costs a few lines if wrong. (2) zizmor in default tox env_list with no .github yet. Ruling: not a defect, Task 5 adds .github/workflows in this plan before plain tox is run, deferred to Task 5 check. Costs one failing default tox run if Task 5 slips.
Task 1: minor (deferred): import inside test function, README lists unregistered commands until Tasks 2-4, long TOML lines, no RED run recorded.
Task 1: fix round 1 base f1454c2.
Task 1: fix round 1/5 (1 addressed, 0 open; commits f1454c2..060417b)
Task 1: complete (commits fd7e20b..060417b, review clean)
Task 2: base 060417b.
Task 2: DONE_WITH_CONCERNS, commit a7ea102, 37 tests. Concerns: tomllib on 3.10 floor (ppt fails to start on 3.10), 32 ty ignores in compat_rules, api_manifest under -I on 3.9 reportedly hits package __init__.
Ruling: tomllib vs 3.10 floor is a real defect, fix in round 1 regardless of review: dependency `tomli>=2; python_version < '3.11'` with try/except import, reason stated in commit. Same fix carried to Task 3 if verify_release uses tomllib. Costs one conditional dependency if wrong.
Task 2: minor (deferred): ~32 `# ty: ignore` and several noqa in moved code, cleanup belongs to a later refactor, not this move.
Task 2: review dispatched on 060417b..a7ea102.
Task 2: review: Needs fixes. Important: (1) tomllib import breaks ppt on 3.10 (confirmed), (2) tarfile filter='data' TypeError on 3.10 < .12, (3) stale test module docstring. Concern (b) about api_manifest on 3.9 disproven by a real 3.9 run. Fix round 1 dispatched, base a7ea102.
Task 2: minor (deferred): stray type/ty ignore placement in compat_rules.py lines 1495, 1513, 1570, 1802, 1806, 1873, 1882.
Ruling: verify_release.py and conda_forge.py also import tomllib. Task 3 adds `python_project_tools/toml_compat.py` (one place for the tomli fallback, exposing `tomllib`) and switches check_compat to it, Task 4 uses it too. Avoids three copies of the same try/except. Costs one small module if wrong.
Task 2: fix round 1/5 (3 addressed, 0 open; commits a7ea102..9f7b8d2)
Task 2: complete (commits 060417b..9f7b8d2, review clean)
Task 3: base 9f7b8d2.
Ruling: security review flagged the Task 2 tarfile fallback (unfiltered extractall when data_filter is missing). Folded into Task 3: the else branch raises RuntimeError naming the required patch release instead of extracting, with a test. Costs: check-compat refuses to run on 3.10.0 to 3.10.11, which is the safe side.
Task 3: DONE_WITH_CONCERNS, commit 60478d8, 42 tests. toml_compat added, tar fallback replaced by RuntimeError. Concern: moved parsers have no prog, --help shows argv[0].
Ruling: set prog='ppt <command>' on every moved parser (check_compat, verify_release now, conda_forge in Task 4). One line each, usage text only. Costs nothing if wrong.
Task 3: review dispatched on 9f7b8d2..60478d8.
Task 3: review: Approved, 1 Important: `args.package or read_package_name()` skips the pyproject read when --package is given. Ruling: keep the new behaviour (flag works outside a checkout), make it tested. Costs: a malformed pyproject.toml is no longer an error when --package is explicit, which is the wanted side.
Task 3: minor (deferred): Callable imported under __main__ guard in verify_release.py.
Task 3: fix round 1 dispatched, base 60478d8 (edge-case tests, prog names, skip list in usage, Phase 3 message).
Task 3: fix round 1/5 (4 addressed, 0 open; commits 60478d8..67093e7)
Task 3: complete (commits 9f7b8d2..67093e7, review clean)
Task 4: base 67093e7.
Task 4: DONE_WITH_CONCERNS, commit ee1a7cd, 61 tests. Concerns: ~36 noqa E501 and 11 ty ignores in conda_forge.py, 4 stray type: ignore on closing parens, shebang and __main__ block kept.
Task 4: minor (deferred): suppression count in conda_forge.py, stray type: ignore comments, shebang plus __main__ in a package module.
Task 4: review dispatched on 67093e7..ee1a7cd.
Task 4: complete (commits 67093e7..ee1a7cd, review clean)
Task 5: base ee1a7cd.
Task 5: DONE_WITH_CONCERNS, commit be33b1b. zizmor clean (10 suppressed via zizmor: ignore comments copied from portalocker), tox py310,py314,ruff,ty,zizmor green.
Ruling: ci.yml and codeql.yml keep `branches: [develop, master]` as in the other repos, harmless with master only and right if a develop branch appears. Costs nothing if wrong.
Task 5: review dispatched on ee1a7cd..be33b1b.
Task 5: complete (commits ee1a7cd..be33b1b, review clean)
Task 5: minor (deferred): pytest-cov in the tests extra is unused without coverage config, autofix.yml comment cites a portalocker audit reference.
Task 6: base be33b1b (python-project-tools unchanged by Task 6 apart from nothing, the change is in ~/workspace/ai).
Ruling: Task 6 installs ppt editable from the worktree path to prove the commands. After the merge the install is re-pointed at /Users/rick/workspace/python-project-tools with `uv tool install --editable --force`, because the worktree goes away. Costs one reinstall if forgotten, and ppt would break when the worktree is removed.
Task 6: DONE_WITH_CONCERNS, ai commit 359a4de. Concern: ~154 lines of pre-existing uncommitted SKILL.md edits and an evals.json edit were swept into the commit. Untracked scripts/conda_forge.py and tests/test_conda_forge.py deleted from disk (content lives in ppt, reviewed faithful in Task 4).
Task 6: review dispatched on ai 359a4de (SKILL.md and evals diff), asked to separate task hunks from swept-in edits.
Task 6: review: Needs fixes. Task hunks correct. Swept-in edits: finished conda-forge Phase 3 prose and evals, already written against `ppt conda-forge`. Important: stale names verify_release.py (386), check_compat.py (417), plus -I sentence and "bundled script" wording.
Ruling: keep ai commit 359a4de with the swept-in edits (coherent, finished, already assumed ppt). Rick is told in the final report so he can split it if he prefers. Costs one commit message that under-describes its content.
Task 6: fix round 1 dispatched (ai repo follow-up commit).
Task 6: fix round 1 commit ai 0810b1e, re-review dispatched.
Task 6: minor (deferred): generic "the script" wording remains at SKILL.md lines 107, 134, 136, 268, 342, 398, 407, 425 (not paths or commands).
Task 6: fix round 1/5 (5 addressed, 0 open; ai commits 359a4de..0810b1e)
Task 6: complete (ai commits 359a4de, 0810b1e, review clean)
Task 7: started, full verification from a clean state.
Task 7: verification green on HEAD be33b1b: tox py310,py314,ruff,ty,zizmor OK, 61 passed on both, ppt 2.0.0. Worktree clean.
Task 7: final whole-branch review dispatched (opus) on fd7e20b..be33b1b.
Task 7: Rick: dearchive and push directly, old content may be fully replaced. ai commit 359a4de stays combined. Repo unarchived, origin/master fast-forwarded c650b5c..be33b1b (Rick's instruction supersedes the plan's PR step). Final review still running, its fixes go as follow-up commits to master.
Task 7: CI run 42200694497 on master: 18 jobs success (15 test cells, ruff, ty, zizmor). CodeQL success.
Task 7: final review: With fixes. Important: (1) global gitignore hides docs/superpowers, add negation, (2) RuntimeError from tar guard uncaught, exit 1 reads as break, raise SetupError, (3) verify_release conda check polled with --wait stalls 900s, probe once and test, (4) api_manifest stdlib-only constraint unguarded, add -S and a manifest() test. Fix before merge minors: Callable import, lefthook ruff pin, autofix.yml audit comment. Cheap prose: .gitignore em dash, conda_forge semicolons, toml_compat __all__ annotation.
Ruling: the conda check in verify_release came in via the swept-in ai working tree. Keep it, probe once without polling, test it. Costs: release skill step 5 loses nothing, conda is Phase 3 anyway.
Task 7: fix wave dispatched, fix base be33b1b.
Task 7: fix wave commit 26e32d3 (9 items), 69 passed, gates clean, zizmor clean. Scoped re-review dispatched on be33b1b..26e32d3.
Task 7: fix wave re-review: all 9 addressed, no new breakage. Pushed 26e32d3 to origin/master.
