# SDD ledger, plan: docs/superpowers/plans/2026-11-06-backfill-tooling.md

Spec: docs/superpowers/specs/2026-11-06-signed-releases-design.md (read).
Worktree: .claude/worktrees/backfill-tooling-plan2, branch worktree-backfill-tooling-plan2.
Base before Task 1: b4cce28.

## Preflight scan

| Pair / task | Produces vs consumes | Found |
|---|---|---|
| T1 / conda_forge, verify_release | `common.Result(name, status, evidence)` vs conda_forge's positional `Result(status, name, evidence)` | T1 converts every call site to keyword arguments, tests cover the outputs |
| T1 / verify_release.check_conda | `fetch_json` now raises `FetchError` on outages, None only on 404 | callers catch FetchError and report FAIL, the existing SKIP-on-None tests stay valid (None means 404) |
| T2, T3, T6, T7, T9 / tests | tests monkeypatch `common.run`, `common.fetch_json`, `common.fetch_bytes` | Ruling: every module does `from python_project_tools import common` and calls `common.x`, never `from common import x`, or the monkeypatch misses. Carried into every dispatch. Costs nothing if wrong |
| T3 / T6, T7, T9 | `tests/fakes.Recorder` prefix matching on joined argv | consistent, keys cut before `--notes-file` so temp paths do not matter |
| T4 / T3 | `versions.match_tag` takes `list[github.Tag]` | versions imports github, github does not import versions, no cycle |
| T7 / T9 | `SignOutcome`, `SignOptions` consumed by backfill report | consistent names and fields |
| T8 / gpg | `create_tag` runs `git tag -s` in a scratch repo under a throwaway GNUPGHOME | the scratch repo needs `user.signingkey` set to the test key, builder detail, test skips without gpg |
| T9 / T8 | backfill creates tags through match_tag and refuses pushes that would fire a workflow | consistent with the `--allow-workflow-trigger` flag on both |
| T9 self | untagged release tag on the root commit, pushed | root commit carries no workflow, push is safe |
| T10 / ai repo | SKILL.md edit in a second repository, local commit only | same pattern as plan 1 Task 6, stage only the skill file |
| T11 | operational, needs Rick | stop point, not a code task |

## Progress
Task 1: DONE_WITH_CONCERNS, commit eb659b5, 81 tests. Concerns: load_mapping outage now FAIL instead of silent {}, --pyproject fallback reads name from the file's parent dir, two helpers extracted from status_feedstock for C901, User-Agent unified.
Task 1: review dispatched on b4cce28..eb659b5.
Task 1: review: Needs fixes. Important: --pyproject name read ignores the given filename, command_status aborts on mapping outage, no tests for conda_forge FetchError paths. Minor: read-phase errors escape fetch_json/fetch_bytes, partial file note, timeout 30 to 60, leftover noqa E501 on touched lines.
Ruling: load_mapping raising on outage is the intended consequence of the FetchError rule (reviewer agrees), command_status records FAIL and continues like command_prepare. Costs: a status run during an outage shows a FAIL row instead of silently mapping names wrong.
Task 1: minor (deferred): verify_release fetch timeout 30 to 60 and unified User-Agent, harmless.
Task 1: fix round 1 dispatched, base eb659b5.
Task 1: fix round 1 commit d8fa42d, 88 tests. Re-review dispatched. Implementer note: command_verify still aborts on a mapping outage because it needs the conda name. Ruling: acceptable, verify cannot proceed without the name and reports FAIL. Costs nothing.
Task 1: fix round 1/5 (5 addressed, 0 open; commits eb659b5..d8fa42d)
Task 1: minor (deferred): fetch_bytes failure message blames the url for local open errors, and it deletes a pre-existing file at the target on failure.
Task 1: complete (commits b4cce28..d8fa42d, review clean)
Task 2: base d8fa42d.
Task 2: DONE, commit 1aef030, 95 tests. Implementer notes REPO_OVERRIDES holds only portalocker (the brief omitted the Global Constraints table with all three). Review dispatched on d8fa42d..1aef030.
Task 2: review: Needs fixes. Critical: REPO_OVERRIDES incomplete (brief lacked Global Constraints). Minor: naive timestamps, download into a missing dir, KeyError on malformed payload.
Task 2: minor (deferred): malformed PyPI payload raises bare KeyError instead of a named error.
Task 2: fix round 1 dispatched, base 1aef030.
Ruling: from now on every implementer dispatch carries the plan's Global Constraints verbatim, the task-brief script extracts only the task section. Costs nothing.
Task 2: fix round 1 commit fc93727, 98 tests, re-review dispatched.
Task 2: fix round 1/5 (3 addressed, 0 open; commits 1aef030..fc93727)
Task 2: complete (commits d8fa42d..fc93727, review clean)
Task 3: base fc93727.
Task 3: DONE, commit e7187d5, 117 tests. gh failures raise RuntimeError. Review dispatched on fc93727..e7187d5.
Task 3: review: Approved. Important: release-trigger regex unanchored to the on: block, over-reports. Ruling: over-reporting refuses a backfill that would have been safe, which is the safe side, and none of the 22 repos has a key named release outside on:. Deferred, slice to the on: block if it ever bites. Costs one false refusal.
Task 3: minor (deferred): flow-style on: {release: ...} missed, no stderr-in-message test, Recorder matches string prefix not tokens.
Task 3: complete (commits fc93727..e7187d5, review clean)
Task 4: base e7187d5.
Task 4: DONE_WITH_CONCERNS, commit f2eee1f, 133 tests. is_version_tag rejects a normalised form still starting with v (packaging tolerates one leading v). Review dispatched on e7187d5..f2eee1f.
Task 4: complete (commits e7187d5..f2eee1f, review clean)
Task 4: minor (deferred): no test pinning match_tag('1.0') against '1.0.0' or sort_key raising on junk, '-' to '.' turns PEP 440 post-release syntax into a dotted segment.
Task 5: base f2eee1f.
Task 5: DONE, commit d511c1d, 141 tests. Review dispatched on f2eee1f..d511c1d.
Task 5: complete (commits f2eee1f..d511c1d, review clean)
Task 5: minor (deferred): no tests for a mixed run, exact-tag release lookup or normalised tag exclusion. main aborts the whole run when pypi.project raises for one package.
Task 6: base d511c1d.
Task 6: DONE, commit 5b2b0c1, 150 tests, gpg tests ran for real. gnupg_home uses mkdtemp because tmp_path exceeds the unix socket path limit on macOS. Review dispatched on d511c1d..5b2b0c1.
Task 6: review: Approved, 1 Important (fixture leaks on setup failure). Fix round 1 dispatched, base 5b2b0c1.
Task 6: minor (deferred): no test for a gpg failure leaving no file, rebuilt comment written verbatim (caller contract).
Task 6: fix round 1 commit 0074fdf, 151 tests, re-review dispatched.
Task 7: base will be 0074fdf once Task 6 closes.
Task 6: fix round 1/5 (2 addressed, 0 open; commits 5b2b0c1..0074fdf)
Task 6: complete (commits d511c1d..0074fdf, review clean)
Task 7: base 0074fdf.
Task 7: DONE_WITH_CONCERNS, commit 85c34ca, 166 tests. No RED run. Conflicts without uploads give status failed. Review dispatched on 0074fdf..85c34ca.
Task 7: review: Needs fixes. Important: (1) conflicts still get signatures uploaded and status signed, (2) a fully signed release re-runs gpg and the interactive sigstore login, (3) workdir cleaned per run not per release. Fix round 1 dispatched, base 85c34ca.
Ruling: footer text stays verbatim even for --gpg-only runs, because a release ends up with both signature kinds (CI sigstore plus local gpg). Costs a footer that is briefly ahead of the assets.
Task 7: minor (deferred): dry run reports kept for existing dists without comparing, missing gpg/uvx/gh binary raises FileNotFoundError outside the handled set.
Task 7: fix round 1 commit c5d2868, 170 tests. Implementer raised: SHA256SUMS regenerated but not re-uploaded when only the .asc is missing (signature would not match the uploaded list), and cleanup lives in _sign_one not sign_release.
Ruling: SHA256SUMS and SHA256SUMS.asc are a derived pair, when either is missing both are regenerated and uploaded with clobber. Costs: an older sums file on a release is replaced, which is wanted since it is derived from the same dists.
Ruling: sign_release owns its per-release workdir (workdir/tag) and removes it in finally, so backfill gets cleanup for free. Fix round 2 dispatched, base c5d2868.
Task 7: fix round 2 commit 9ff92fc, 171 tests. Combined re-review of 85c34ca..9ff92fc dispatched.
Task 7: minor (deferred): dry run does not model the sums pair rule, can under-report one upload.
Task 7: fix rounds 1-2/5 (7 addressed, 0 open; commits 85c34ca..9ff92fc)
Task 7: complete (commits 0074fdf..9ff92fc, review clean)
Task 7: carry to Task 9: sign_release removes workdir/<tag>, so rebuilt files handed to it must live outside that directory. Footer edit can happen on an already signed outcome, harmless.
Task 8: base 9ff92fc.
Task 8: DONE, commit 3a3c38f, 181 tests, gpg signed-tag test ran. Review dispatched on 9ff92fc..3a3c38f.
Task 8: review: Approved, 1 Important (no existing-tag pre-check). Fix round 1 dispatched, base 3a3c38f.
Task 8: minor (deferred): GENERATED names skipped even when tracked, stray top-level sdist files ignored, local date in the tag message.
Task 8: fix round 1 commit 31f7523, 182 tests, re-review dispatched.
Task 8: fix round 1/5 (2 addressed, 0 open; commits 3a3c38f..31f7523)
Task 8: complete (commits 9ff92fc..31f7523, review clean)
Task 9: base 31f7523.
Task 9: DONE, commit 02e8164, 210 tests. Concerns: no git fetch --tags before changelog lookup, setuptools-scm rebuilds fail (no .git in archive), untagged group is one row, versions without files and tag count as skipped (exit 1).
Task 9: review dispatched on 31f7523..02e8164.
Task 9: review: Needs fixes. Important: dry-run test lacks rec.calls == [] and fakes sign_release, containment too broad (LookupError) and too narrow (OSError, TarError, UnicodeDecodeError escape), no git fetch --tags so missing local tags give permanent stock notes, pre-existing local tag blocks a version on every rerun. Fix round 1 dispatched, base 02e8164.
Task 9: minor (deferred): untagged release body lists only first-run versions, dry run cannot preview untagged demotion, footer says exact PyPI uploads even with rebuilt files listed (footer names them as rebuilt in the same text), rebuilt releases recompared every run.
Task 9: fix round 1 commit 2743b3b, 220 tests, re-review dispatched (pyright flags backfill.py:400 unreachable, handed to the re-reviewer).
Task 9: fix round 1/5 (5 addressed, 0 open; commits 02e8164..2743b3b)
Task 9: complete (commits 31f7523..2743b3b, review clean)
Task 9: minor (deferred): dead `return None` at the end of _plan_tag, OSError from a missing git binary in _fetch_tags gives a traceback not exit 2, dry run skips the fetch.
Task 10: base 2743b3b.
Task 10: DONE, commits 933cd1f (worktree) and ai 26c0de2 (SKILL.md), 224 tests, prose grep clean. Concern: sign step 2b placed before step 3 (release creation).
Task 10: review dispatched on 2743b3b..933cd1f plus ai 26c0de2.
Task 11 prerequisites: Rick uploads the GPG key himself (asked, pending). python-progressbar release workflow disabled by me (gh workflow disable release, state disabled_manually), approved by Rick.
Task 10: review: Needs fixes. Critical: sign step placed before the release exists (2b, must be 3b). Important: --gpg-only in the skill step conflicts with verify-release's default signature check, step text omits SHA256SUMS, step 5 list not updated.
Ruling: until plan 3 makes CI produce Sigstore bundles, the skill step runs the full `ppt sign vX.Y.Z` (gpg plus interactive sigstore). Plan 3 switches it to --gpg-only. Costs one browser login per release until then.
Task 10: fix round 1 dispatched, base 933cd1f (worktree) and ai 26c0de2.
Task 10: fix round 1 commits 1ccba84 (worktree) and ai 387972c, 224 tests, re-review dispatched.
Task 10: fix round 1/5 (4 addressed, 0 open; commits 933cd1f..1ccba84, ai 26c0de2..387972c)
Task 10: complete (commits 2743b3b..1ccba84 plus ai 26c0de2, 387972c, review clean)
Task 10: minor (deferred): SKILL.md 3b refers to "plan 3", an internal name.
Branch gate on 1ccba84: tox py310,py314,ruff,ty,zizmor OK, 224 passed on both. Worktree clean, 18 commits since b4cce28.
Final whole-branch review dispatched (opus) on b4cce28..1ccba84.
Smoke test on real data from the worktree (read-only): `ppt inventory tissue` shows 11 versions, 7 tagged v0.1..v0.7, 4 untagged (0.8, 0.9, 0.9.1, 0.9.2), 0 releases, matches the recon. `ppt backfill tissue --checkout ~/workspace/tissue --dry-run` plans 11 versions, creates nothing, Latest none/none OK. Cosmetic: "would download 1 files".
Final review: With fixes. Critical: (1) tissue and 5 other in-scope repos are archived, preflight does not check, every mutation would be refused (spec gap). (2) Latest guard blind when a repo has no releases (tissue), latest_release cannot tell 404 from error. Important: tag-trigger detection misses bare push/create, release-trigger check reads only the default branch, compare ignores sdist files untracked at the sha, one sigstore login per release not per package, rebuilds not reproducible so reruns conflict, footer wording wrong for rebuilt and multi-version untagged releases.
Ruling: when no release existed before the run, the Latest target is the highest stable version that got its own release (not the untagged anchor), set explicitly with `gh release edit TAG --latest` at the end, and asserted. Rick is told and can override. Costs: one explicit edit per repo that had no releases.
Ruling: one sigstore invocation per package, batched across releases in chunks of at most 100 files, is required by the spec prerequisites (one login per package). Implemented in the fix wave. Costs: more code in backfill and sign.
Ruling: unarchiving the six archived repos is Rick's call (asked). preflight refuses archived repos with exit 2 naming gh repo unarchive.
Final fix wave dispatched (opus), fix base 1ccba84.
Rick: dearchive all six archived repos and leave them open (done: tissue, zfs-utils-osx, spotimote, supervisord-slack-notifier, mailjet, sphinx-pypi-upload). Rick agrees with the Latest ruling (highest stable version with its own release becomes Latest when none existed).
Prerequisites now met: six repos unarchived (isArchived false each), GitHub GPG key refreshed by Rick (expires 2031-02-01), python-progressbar release workflow disabled. Waiting on the final fix wave.
Final fix wave: commit e85634c, 301 tests, gates clean. Fixer's safe-side calls: preflight refuses when repo metadata unreadable, run stops when Latest unreadable after the first create, release-trigger refusal unless the workflow is listed disabled, footer waits for the bundles, rebuild skip honours the enabled signature kinds, match-tag gained --gpg-key.
Final fix wave concerns (deferred, watch in the report): tracked setup.cfg with sdist-added [egg_info] will fail byte comparison and send versions to untagged, `--only` on a repo without releases locks that version as Latest, rebuild skip also applies under --replace-assets, a run stopped by the Latest guard leaves gpg-only releases to be completed by a rerun.
Scoped re-review dispatched on 1ccba84..e85634c.
Gate on e85634c: tox py310,py314,ruff,ty,zizmor OK, 301 passed. Tissue dry run on e85634c: 11 rows, nothing created, Latest none/none/target none OK. Worktree clean.
Final fix wave re-review: all 16 addressed, no new Critical/Important breakage. Three residual minors parked with rulings:
Ruling: Latest candidate set is run.created, so a rerun after an interrupted first run marks nothing. Parked: safe direction (no Latest rather than a wrong one), fix before Step 4 by widening to every stable plan with its own release. Costs a repo left without Latest after an interrupted run, repairable with gh release edit --latest.
Ruling: the rebuild skip does not check the footer marker, a failed edit_notes on a rebuild plan is never retried. Parked: rare path (bundles uploaded, notes edit failed), fix before Step 4 by requiring the marker. Costs one missing footer.
Ruling: _triggers_on_release misses block-list `- release`. Parked: tissue has no workflows, over-refusal otherwise safe, fix before Step 4. Costs a release event firing on a repo with that exact YAML shape.
Final gate green on e85634c (301 passed, ruff, ty, zizmor). Pushing to master per Rick's rule for this repo.
