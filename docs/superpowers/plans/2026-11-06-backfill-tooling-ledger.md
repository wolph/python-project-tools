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
Post-merge: CI run 42218876233 on e85634c failed on 3.10 (git %cI emits a Z suffix on the runners, fromisoformat on 3.10 rejects it) and on Windows (gpg key generation exits 2 in the gnupg_home fixture). Fix d2771c3: common.parse_iso_datetime shared by pypi and match_tag, gnupg_home skips on win32 or when gpg cannot generate a key. Local tox py310,py314,ruff,ty green, gpg tests still run here. Pushed to master, CI watched.
Inventory before the canary (20 of 22 packages, read-only): 564 versions, 562 with files, 150 untagged. `formatter` (PyPI) points at WoLpH/formatter which does not exist, its versions 1.0.0 to 1.0.3 overlap formatter2's 1.0.3 in WoLpH/python-formatter (0 tags). Needs Rick's call. Several old releases already carry .asc files from the old release tooling, backfill keeps them.
Canary preparation: tissue 0.8 did not match after the I3 strictness because setuptools appends [egg_info] to a tracked setup.cfg. Fix f71febe strips that block (0.8 matches b4cf9b9 again). tissue 0.9.1 and 0.9.2 still differ in setup.cfg because old setuptools rewrites the whole file through configparser (key=value spacing, blank lines).
Ruling: setup.cfg is compared as configuration (configparser, tag_* keys of egg_info dropped), every other file byte for byte. Costs: a setup.cfg change that only touches whitespace or comments no longer counts as a difference, which is the intended tolerance.
Fix ffe63c4: setup.cfg compared as configuration. tissue preview after it: 0.8 -> b4cf9b9, 0.9.1 -> 3105949, 0.9.2 -> 893b4dd, 0.9 untagged (real difference). Rick: skip the PyPI package `formatter` (predecessor of formatter2, tag v1.0.3 would collide). Rick: run the canary now with him present for the Sigstore login.
Canary run 1 on tissue: stopped by the Latest guard right after creating v0.1 (empty release). GitHub marks the first release of a repo Latest despite --latest=false, which answers the review's open question. Nothing signed, no tags created. The empty v0.1 release was deleted (our own artefact, minutes old).
Ruling: in a repo without releases the target version (highest stable with files) is processed first so it becomes Latest, then the rest oldest first, and the guard after the first creation expects exactly the target tag. Costs: the first release of such a repo is the newest version, which is the wanted outcome.
Fix f741a38: backfill releases the Latest target first on a repo without releases, guard expects exactly the target, target failure stops the run before anything older is created. 326 tests, tox py310,ruff,ty green. Dry run on tissue: target v0.9.2, order 0.9.2 then 0.1..0.9.1. Pushed to master.
Canary run 2 on tissue (f741a38): 11 versions signed, 4 tags created (v0.8 -> b4cf9b9, v0.9.1 -> 3105949, v0.9.2 -> 893b4dd, pypi-untagged-releases -> 3e65453), 0.9 on the untagged release, one Sigstore login, Latest none -> v0.9.2 = target, exit 0. Independent checks on v0.9.2: sha256 equal to PyPI, GPG good on the sdist, SHA256SUMS and all four tags, sigstore verify identity OK for wolph@wol.ph with the GitHub issuer. Run 3 (idempotency): every row already signed, 0 uploaded, exit 0. VERIFY.md identity filled in.
Batch 1 stopped on Sphinx-PyPI-upload-2 (exit 2, nothing created): the only PyPI version 0.2.2 has no matching commit, so the Latest target could not get its own release and the target-first rule stopped the run. Four tagged versions without PyPI files (rebuild) were skipped.
Ruling: the target is chosen through a fallback chain, highest stable candidate first, falling through when a candidate ends untagged or skipped without creating anything, stopping only after a partial creation. With no candidate at all the anchor is the only release and becomes Latest. Costs: a rebuilt release can become Latest while a newer version sits on the untagged anchor, which is the correct reading of "highest stable with its own release".
Sphinx-PyPI-upload-2 0.2.2 verified by hand: the sdist setup.py reads README.rst, commit 94e4948 reads README, so the upload came from an uncommitted working tree. Untagged is the right outcome, not a comparison bug.
Fix e6261a9: Latest candidate fallback chain, 330 tests, tox py310,ruff,ty green, pushed to master. Behaviour change noted by the fixer: a target whose signing fails after its release exists now stops the run (exit 2) instead of continuing.
Batch 1b: Sphinx-PyPI-upload-2 blocked on the Sigstore browser login since 14:19. Releases 0.1, 0.1.1, 0.2, 0.2.1 (rebuilt) and the untagged anchor (0.2.2, PyPI bytes) already exist with GPG signatures and signed SHA256SUMS, bundles and footers pending. GitHub reports the anchor as Latest mid-run although the first created release was 0.2.1, to investigate after the end-of-run mark_latest and assertion.
Cause of the Latest drift: releases on old tags carry the tag's 2009 date as created_at, the anchor's new tag a 2026 date, and GitHub's automatic (legacy) Latest is recomputed from dates and versions. Fix ad1a0c5: pin the target with gh release edit --latest right after the guard accepts it. 330 tests, tox py310,ruff,ty green, pushed to master. The running batch keeps its loaded code, the next packages pick up the fix.
Rick at 16:12: the login was done hours ago. Checked: the sigstore process (pid 459) still listened on 127.0.0.1:53087, the OAuth callback never arrived. Killed it at 16:13. Run ended exit 1, five releases GPG-only, Latest correctly 0.2.1 (end step marked it). Rebuild reproducibility test of 0.2.1: wheel identical, sdist differs, so a plain rerun would conflict.
Ruling: a rerun resumes a rebuilt release from its own assets, verified by sha256 against the GPG-verified SHA256SUMS on the release, instead of rebuilding. Sigstore gets a 900 s timeout. Costs: trust in the release's own SHA256SUMS, which only our key can sign.
Fix 7384bad: resume a rebuilt release from its own assets (gpg VALIDSIG on SHA256SUMS for the configured key, sha256 per dist), footer marker required for already signed, sigstore 900 s timeout with SIGTERM then SIGKILL (uv forwards SIGTERM, SIGKILL orphans the grandchild), timeout keeps the report. 350 tests, tox py310,py314,ruff,ty green. Read-only smoke on sphinx-pypi-upload: four rebuilt releases resume with the real key, wrong key refused. Pushed to master.
Deferred: a PyPI plan whose release has a SHA256SUMS not listing its PyPI files should be refused (only reachable with a wrong package name), footer-only completion reports already signed, double download during resume.
Batch 1c: Sphinx-PyPI-upload-2 resumed from release assets as designed, sigstore login prompt at 16:29, again no callback, timeout fired cleanly at 16:44 (exit 1, report intact, Latest 0.2.1 OK). CI on 7384bad: only macos 3.10 cancelled, rerun requested.
18:40 Sphinx-PyPI-upload-2 completed on retry: login arrived, 4 rebuilt releases resumed from their own assets (no rebuild), anchor completed, 9 bundles, footers on all 5, Latest 0.2.1 OK, exit 0.
18:41 slack_notifier done (Latest v1.0.0, exit 0). cache_toolbar stopped (exit 2): right after creating v1.0.1 GitHub returned no Latest, a second later v1.0.1. Eventual consistency, the guard read too early. Only an empty v1.0.1 release exists, rerun-safe.
Rick: does the login have to happen for every package? Ruling: one Sigstore session per batch run. backfill takes several packages in one invocation, defers all bundles across packages to one sign step at the end, and the Latest guard polls with a short delay before deciding. Costs: a longer single run per batch, a failed login fails the whole batch's bundles, which a rerun completes.
Commits 7b1ae23 (Latest poll, 5 x 2 s) and aa27372 (ppt backfill --batch FILE, one sigstore session for all packages, per-package isolation, summary table, new module backfill_batch.py). 376 tests, tox py310,py314,ruff,ty green. Pushed to master. Fixer notes: a stopped single-package run now still signs its bundles, same-named files across packages cost an extra login, unexpected exceptions still end the batch.
CI on aa27372 failed on all Windows jobs: test_parse_batch_expands_the_home_directory set only HOME, expanduser reads USERPROFILE on Windows. Test fix 45626b5 pushed (batch tests 21 passed locally). The running batch is unaffected, it uses aa27372 code.
19:05 batch of 17 mid-run (output buffered to file, only visible at the end): python-formatter 6/6, mailjet 7/7, speedups 12/15 releases carry SHA256SUMS. django-admin-generator, python-statsd, django-statsd, django-utils, django-tags-input, mt940, portalocker, python-utils: 0 signed so far, all have an active publish.yml on tag push v*. No workflow run was triggered on any of the 16 repos since 17:50 UTC.
Batch of 17, refusals found mid-run: (1) django-admin-generator, python-statsd, portalocker: release-trigger regex matched a job named release in publish.yml (on: push tags only), false positive. (2) django-statsd, django-utils2, django-tags-input, mt-940, python-utils: git fetch origin --tags rejected, local tags point at other commits than GitHub's (mt940 v4.11.0, v4.13.0, v4.26.0 and more).
Ruling: decide release triggers from the parsed on block only. Fetch GitHub tags into refs/ppt/remote-tags/* and resolve every tag through it, refuse to create a tag whose name exists locally, report diverging local tags without touching them. Costs: a private ref namespace in the user's checkouts (harmless, removable with git for-each-ref plus update-ref -d).
19:14 batch of 17 reached the shared sign step: 640 files, login prompt, no login, timed out about 19:29. Second chunk of 5 files (same basenames across packages) prompted at 19:29. Fixer for the refusals: problem 1 committed 3af96ca, problem 2 implemented with 412 tests green.
Fixer commits 0aa4213 (release trigger from the on value only) and 42c42b3 (GitHub tags via refs/ppt/remote-tags, local same-name tag refused, divergence note). 412 tests. Dry runs on fresh clones of django-admin-generator and mt940 pass preflight.
Concern: candidates came from git log --all, so a local-only commit could be tagged and pushed. Audit of the 25 tags created today: every tagged commit is on a remote branch, nothing private was published.
Ruling: candidates only from refs/remotes/origin/* and refs/ppt/remote-tags/*, plus a reachability refusal in create_tag. Costs: a release cut from a never-pushed commit stays untagged, which is the wanted side.
Fix bf32d14: candidates only from refs/remotes/origin/* and refs/ppt/remote-tags/*, create_tag refuses commits GitHub lacks, anchor root picked from the same refs. 420 tests, tox py310,py314,ruff,ty,zizmor green. 0aa4213, 42c42b3, bf32d14 pushed to master. Main checkout not updated until the running batch ends.
19:44 batch of 17 run 1 ended exit 2. Latest OK on all nine processed packages. Releases with files and GPG on cache_toolbar, spotimote, zfs-utils-osx, django-redis-admin, formatter2, mailjet, speedups, numpy-stl, progressbar2, bundles pending (no login). Eight packages refused (release-trigger false positive, diverging local tags), both fixed on master. Some ancient versions fail to rebuild under current setuptools (reported failed). Run 2 started with bf32d14.
CI on bf32d14 failed on Windows only: scratch_repo wrote CRLF via text mode. Test fix 5c232c2 (newline='\n'), 420 passed locally, pushed.
20:41 batch run 2 ended exit 2. Latest OK on all 16 processed packages. All releases created or completed with files, .asc and signed SHA256SUMS. Both Sigstore steps (1144 files, then 6) timed out without a login. No workflow run was triggered on any repo. python-statsd refused correctly (publish.yml on release: published). Some rows hold conflicting assets: an existing release asset with the same name and different bytes, left alone (needs Rick's call on --replace-assets). A few ancient versions fail to rebuild or carry absolute symlinks in the tree.
Rick 21:32: rerun now (present for the login), python-statsd publish.yml disable during the run and re-enable after (disabled_manually confirmed), conflicting assets replaced with the PyPI bytes (--replace-assets). Run 3 started on 5c232c2.
21:57 run 3: GitHub API rate limit exceeded during python-utils, numpy-stl and progressbar2 skipped at preflight. Sigstore prompt for 905 files. Rick reports Internal Server Error in the browser, the local listener is still waiting, so the error is on the OAuth side. Told Rick to retry the GitHub login in the same tab.
Commit f1581f6: BROWSER points sigstore's webbrowser.open at python_project_tools.browser_relay, which logs the local login URL (http://localhost:PORT/) and then opens the real browser. tox py310,py314,ruff,ty green, pushed.
22:27 run 3 ended exit 2. Both Sigstore steps (905 and 1 file) timed out without login (Rick saw Internal Server Error on the OAuth side). Replace-assets re-uploaded our own .asc and SHA256SUMS where they differed (gpg signatures are not deterministic), no conflicting rows left. Latest could not be read at the end: GitHub's secondary rate limit (HTTP 403 rate limit exceeded while core still shows 4875 remaining) hit the read, report says CHANGED with after unknown. Must re-verify every Latest once the limit clears. numpy-stl and progressbar2 skipped at preflight by the same limit. python-statsd publish.yml still disabled.
Rick 23:41: run 4 tonight. Limit clear, run 4 started on f1581f6 without --replace-assets.
23:56 run 4: login arrived through the relay URL at once, certificate issued. sigstore sign on 1247 files in one invocation hit HTTP 429 from rekor.sigstore.dev after 525 log entries and 728 TSA 429 warnings, exit 1, no bundle written for any file. Latest OK on all 17. 
Ruling: replace the sigstore CLI call with an own signer run under the pinned sigstore library: one OIDC token per run (through the relay), files signed one at a time with a pause and retry with backoff on 429, each bundle written immediately so partial progress survives and a rerun only signs what is missing. Costs: a slower signing step (about 2 files a second, roughly 12 minutes for 1247 files) and our own code on the sigstore library API.
Commits 4544d8b (own throttled resumable signer python_project_tools/sigstore_signer.py run under uvx --from sigstore==4.5.1, one token per run, 0.5 s pace, 429/5xx back-off, bundle written per file, digest-checked resume, one sign call per run) and a timeout bump to 6 s per file. 459 tests, tox py310,py314,ruff,ty,zizmor green, --help smoke in the pinned env passes. Pushed to master. Open: token lifetime unknown, only a real run measures it, resume covers it.
02:46 run 5 ended: login link http://localhost:55740 never used (Rick away), signer timed out after the scaled limit. No bundles, nothing else changed. Waiting for Rick to start run 6 with a fresh link.
Rick 11:59: run 6 now with one login.
14:35 run 6 ended: link http://localhost:55163 not used, timeout, nothing changed.
Rick 14:36: mailjet stays archived (GPG only, no bundles), run 7 now without mailjet.
22:47 run 7: login arrived, 338 of 1244 signed in about 8 minutes, then the identity token expired (exit 3 path). Token lifetime is about 10 minutes.
22:49 run 7 ended exit 1: 27 releases completed with bundles and footer, the other bundles of partly signed releases were lost with the temp workdir. Added to the signer fixer: upload every existing digest-checked bundle, footer only when complete.
Commits 3c86003 (re-login on token expiry up to 6, 900 s per login, 3 workers with shared back-off) and c8f39b3 (salvage uploads every covering bundle of partly signed releases). 472 tests, tox py310,py314,ruff,ty,zizmor green, pinned-env smoke ok. Pushed.
23:33 run 8: first login link http://localhost:49788 not used within 900 s, signer exit 5, 0 of 953 signed.
2026-11-12: macOS cleared the loose files of the session scratchpad (batch lists, logs, plan2-ledger.md). This ledger was rebuilt from the committed d361ce6 copy plus the 41 appends recorded in the session transcript, in order.
Rick 14:19: run 9 now. Batch reduced to the 11 repos with missing bundles (audit by asset names: a dist .asc without a .sigstore.json next to it).
14:41 run 9: one login, 953 of 953 signed with three workers, no token expiry. All releases of the 11 repos complete except 20 progressbar2 releases whose 2017 to 2019 GitHub uploads differ in bytes from PyPI (conflicting assets left alone).
Rick 14:40: replace the progressbar2 conflicts with the PyPI bytes, as in run 3. The old copies (74 files, 20 releases) were downloaded with their sha256 list into the scratchpad first.
15:08 python-statsd publish.yml re-enabled after run 9, gh workflow list shows it active.
15:14 progressbar2 run with --replace-assets: login not used within 900 s, but the PyPI bytes, their .asc and SHA256SUMS were uploaded before signing. v3.34.2 checked: sha256 equal to PyPI, gpg Good signature. Already complete releases still verify (v4.7.0: gpg, sha256sum -c and sigstore verify identity OK).
Ruling: --replace-assets also re-signs releases that are already complete, 208 files instead of 40. Parked: harmless (same bytes, own signatures of the same week), costs one longer signer run when the flag is used, fix by skipping complete releases before the replace logic.
Ruling: speedups 1.1.0 (py3.11 macOS .egg) and portalocker 2.7.0 (py3-none wheel) carry files Rick uploaded in 2023 with his own .asc of that day, not on PyPI. Left alone, outside the backfill. Costs nothing, the audit lists them by name.
15:48 and 17:27 progressbar2 runs without --replace-assets: no tab seen by Rick, link not used, nothing changed.
18:20 progressbar2 run: login at once, 40 of 40 signed, 109 of 109 releases complete. Final audit: every release of the 19 repos in scope complete (bundle per dist, footer, signed SHA256SUMS), Latest unchanged and correct everywhere. mailjet stays archived with GPG only.
