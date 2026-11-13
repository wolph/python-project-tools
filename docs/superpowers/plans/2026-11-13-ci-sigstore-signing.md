# CI Sigstore signing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `ppt sign vX.Y.Z` signs a release completely without a browser login, by dispatching a `sign.yml` workflow for the Sigstore bundles and adding GPG locally.

**Architecture:** A workflow template (`templates/sign.yml`) signs the PyPI files of a release with the runner's OIDC identity and uploads only the bundles and missing dists. `ppt sign` checks every bundle on the release against two accepted identities, dispatches sign.yml and waits for it when bundles are missing, then does the GPG half and `SHA256SUMS` as today. Identity checks run in the pinned sigstore environment through one helper script, the same pattern `sigstore_signer.py` uses.

**Tech Stack:** Python 3.10+, argparse, `gh` CLI, `uvx --from sigstore==4.5.1`, GitHub Actions, `sigstore/gh-action-sigstore-python` v3.5.0, pytest, ruff, ty, actionlint.

**Spec:** `docs/superpowers/specs/2026-11-06-signed-releases-design.md`, section 4 (revised 2026-11-13), plus decision 5, the `ppt sign` bullet in section 2, section 5, the prerequisites and the risks.

## Global Constraints

- Branch `ci-sigstore-signing` in python-project-tools. Commits GPG signed (already configured).
- CI does not depend on ppt. sign.yml uses only the GitHub runner, `gh`, `python3` stdlib, `astral-sh/setup-uv` and the sigstore action.
- sign.yml trigger: `workflow_dispatch` only, inputs `tag` and `package` required, plus `request_id` (optional, default `''`, used only to find the run).
- sign.yml permissions: `contents: write` and `id-token: write`, nothing else. Workflow-level `permissions: {}`.
- Never `--clobber` in sign.yml. An asset with the same name and different bytes fails the run and names the asset.
- sign.yml does not write `SHA256SUMS`. `ppt sign` owns `SHA256SUMS` and `SHA256SUMS.asc` as a pair.
- Accepted identities, per dist file:
  - CI: SAN `https://github.com/<repo>/.github/workflows/sign.yml@refs/heads/<any branch>`, issuer `https://token.actions.githubusercontent.com`, trigger `workflow_dispatch`.
  - Rick: SAN `wolph@wol.ph`, issuer `https://github.com/login/oauth`.
- Sigstore pin stays `sigstore==4.5.1` (`signing.SIGSTORE_ARGV`). Action pin: `sigstore/gh-action-sigstore-python@790bc6befb9d733738f18d8f895854b453640ec9 # v3.5.0`.
- The browser signer is opt-in only, behind `--sigstore-browser`. The skill passes it only for a repository whose sign.yml is not merged yet (exit status 3 tells it so).
- `ppt sign --all` dispatches only for releases created on or after the date of the first commit of `.github/workflows/sign.yml` on the default branch. Older gaps and rebuilt files are reported, not dispatched.
- Explicit type annotations on every variable and signature, as in the existing modules. ASCII punctuation, no semicolons, British spelling in prose and docs.
- Gate per task: `uv run pytest > $TMPDIR/ppt-test.log 2>&1; echo $?` must print 0 (never pipe pytest through tail or head), `uv run ruff check`, `uv run ty check`.

## Review Focus

1. Repository names that differ only in case (`wolph/python-utils` from a URL against `WoLpH/python-utils` in a certificate) must match the CI identity. Test in Task 1.
2. A `tag` or `package` input carrying shell metacharacters must not reach a shell unquoted in sign.yml: inputs go through `env:`, never `${{ }}` inside `run:`, and are validated first. Test in Task 2.
3. Two dispatches in flight at once (a retry while the first still runs) must each find their own run, never the other one. Test in Task 3.
4. A bundle asset that exists on the release but verifies against no accepted identity must stop `ppt sign` with its name, not trigger a dispatch whose upload would then collide. Test in Task 4.
5. A release in a repository without sign.yml must end with exit status 3 and a message naming the template, without having uploaded any GPG file first, so the browser rerun starts from a clean release. Test in Task 4.

---

### Task 1: Bundle identity verification

**Files:**
- Create: `python_project_tools/bundle_verifier.py` (runs inside the sigstore environment, stdlib at import time, like `sigstore_signer.py`)
- Create: `python_project_tools/identities.py`
- Test: `tests/test_bundle_verifier.py`, `tests/test_identities.py`

**Interfaces:**
- Produces, in `bundle_verifier.py`:
  - `CI_ISSUER: str = 'https://token.actions.githubusercontent.com'`, `RICK_ISSUER: str = 'https://github.com/login/oauth'`, `RICK_IDENTITY: str = 'wolph@wol.ph'`
  - `ci_identity_matches(san: str, repo: str) -> bool`: pure, stdlib.
  - `main(argv: list[str]) -> int`: `--manifest FILE --repo OWNER/NAME --output FILE`. Manifest is a JSON list of `{"name": str, "sha256": str, "bundle": str}`. Output is a JSON object `{name: "ci" | "rick" | null}`. Exit 0 when it ran, whatever the verdicts, 2 on usage errors.
- Produces, in `identities.py`:
  - `Identity = typing.Literal['ci', 'rick']`
  - `@dataclasses.dataclass(frozen=True) class BundleCheck: name: str, sha256: str, bundle: pathlib.Path`
  - `verify_bundles(checks: list[BundleCheck], repo: str) -> dict[str, Identity | None]`: one `uvx` run for all checks, raises `signing.SigningError` when the helper exits non-zero.

- [ ] **Step 1: Write the failing tests for `ci_identity_matches`**

```python
@pytest.mark.parametrize(
    ('san', 'expected'),
    [
        ('https://github.com/WoLpH/python-utils/.github/workflows/sign.yml@refs/heads/develop', True),
        ('https://github.com/WoLpH/python-utils/.github/workflows/sign.yml@refs/heads/master', True),
        ('https://github.com/wolph/PYTHON-UTILS/.github/workflows/sign.yml@refs/heads/develop', True),
        ('https://github.com/WoLpH/python-utils/.github/workflows/publish.yml@refs/heads/develop', False),
        ('https://github.com/WoLpH/python-utils/.github/workflows/sign.yml@refs/tags/v4.2.1', False),
        ('https://github.com/WoLpH/python-utils/.github/workflows/sign.yml@refs/heads/', False),
        ('https://github.com/WoLpH/numpy-stl/.github/workflows/sign.yml@refs/heads/develop', False),
        ('https://github.com/WoLpH/python-utils-evil/.github/workflows/sign.yml@refs/heads/develop', False),
    ],
)
def test_ci_identity_matches(san: str, expected: bool) -> None:
    assert bundle_verifier.ci_identity_matches(san, 'WoLpH/python-utils') is expected
```

- [ ] **Step 2: Run them, expect FAIL (module missing)**

Run: `uv run pytest tests/test_bundle_verifier.py -v`

- [ ] **Step 3: Implement `ci_identity_matches` and `main` in `bundle_verifier.py`**

Repo comparison is case-insensitive, the branch part must be non-empty. `main` imports sigstore inside the function: `Verifier.production()`, `Bundle.from_json(path.read_bytes())`, `Hashed(algorithm=HashAlgorithm.SHA2_256, digest=bytes.fromhex(sha256))`, and for each entry tries the CI policy first, then the Rick policy. CI policy: `AllOf([OIDCIssuerV2(CI_ISSUER), GitHubWorkflowTrigger('workflow_dispatch'), _SignYmlSan(repo)])`, where `_SignYmlSan` is a `VerificationPolicy` that reads the certificate's URI SAN and calls `ci_identity_matches`. Rick policy: `Identity(identity=RICK_IDENTITY, issuer=RICK_ISSUER)`. A `VerificationError` or `InvalidBundle` means that policy gave `null`.

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Write the failing tests for `verify_bundles`**

`test_verify_bundles_runs_helper_once`: Recorder with a canned reply for `uvx --from sigstore==4.5.1 python -I`, whose side effect writes `{"a.whl": "ci", "a.tar.gz": null}` to the `--output` path. Assert one call, argv contains `--repo WoLpH/x`, result equals that dict. `test_verify_bundles_raises_on_helper_failure`: returncode 1 raises `signing.SigningError`. Add a `write_output` hook to the Recorder reply in `tests/fakes.py` if the existing one cannot write files (a callable per key, called with the argv).

`test_real_backfilled_bundle_verifies_as_rick` (network): fetch `tissue-0.9.2.tar.gz.sigstore.json` from the WoLpH/tissue release `v0.9.2` and the sdist's sha256 from PyPI JSON, run `verify_bundles` for real, expect `'rick'`. Skip with `pytest.skip` when `gh` or the network is unavailable. Register no new marker.

- [ ] **Step 6: Implement `verify_bundles` in `identities.py`**

Manifest and output in a `tempfile.TemporaryDirectory(prefix='ppt-verify-')`, argv `[*signing.SIGSTORE_ARGV, str(VERIFIER_SCRIPT), '--manifest', ..., '--repo', repo, '--output', ...]`.

- [ ] **Step 7: Run the gate, expect PASS, commit**

```bash
git add python_project_tools/bundle_verifier.py python_project_tools/identities.py tests/test_bundle_verifier.py tests/test_identities.py tests/fakes.py
git commit -m "feat: verify Sigstore bundles against the CI and Rick identities"
```

---

### Task 2: The sign.yml template

**Files:**
- Create: `templates/sign.yml`
- Modify: `pyproject.toml` (add `pyyaml>=6` to the `tests` extra)
- Test: `tests/test_sign_template.py`

**Interfaces:**
- Produces: the file `templates/sign.yml`, copied byte for byte into each repository as `.github/workflows/sign.yml`. Its `name:` is `Sign release` (VERIFY.md passes it as `--name`). Its `run-name` is `Sign ${{ inputs.tag }} ${{ inputs.request_id }}`, which Task 3 relies on to find a run.
- The inline Python between the lines `# ppt-plan-begin` and `# ppt-plan-end` is the "plan" step. Environment in: `TAG`, `PACKAGE`, `PYPI_JSON_URL` (default `https://pypi.org/pypi/{package}/{version}/json`, overridable for tests), `ASSETS_JSON` (path to `gh release view --json assets` output), `EXISTING_DIR`, `DIST_DIR`, `GITHUB_OUTPUT`. Output keys: `to_sign` and `to_upload`, space-separated file names.

Job shape, in order:
1. Validate inputs from `env:`: `TAG` must match `^v?[0-9][0-9A-Za-z.+!-]*$`, `PACKAGE` must match `^[A-Za-z0-9][A-Za-z0-9._-]*$`. Otherwise exit 1.
2. `gh release view "$TAG" --json assets > assets.json`, which fails the run when there is no release.
3. `gh release download "$TAG" -D existing -p '*.whl' -p '*.tar.gz' -p '*.zip'`, tolerating "no assets match".
4. The plan step: download every PyPI file of the version into `dist/`, check each sha256 against the JSON, compare each dist already on the release with its PyPI sha256 (mismatch exits 1 naming it), then emit `to_sign` (dists without a `<name>.sigstore.json` asset) and `to_upload` (dists not on the release). PyPI with no files exits 1. Version is `TAG` without a leading `v`.
5. The sigstore action on `to_sign` (skipped when empty), with `upload-signing-artifacts: false` and `release-signing-artifacts: false`.
6. `gh release upload "$TAG"` for `to_upload` and `dist/<name>.sigstore.json` for each name in `to_sign`, never `--clobber`.

The rule "skip a file whose bundle verifies" from the spec becomes "skip a file that has a bundle asset". `ppt sign` checks the identity (Task 4) and stops on an invalid one, which needs a person in both versions. That keeps the sigstore CLI out of the workflow.

- [ ] **Step 1: Write the failing tests**

- `test_template_triggers_and_permissions`: `yaml.safe_load` the template. `on` (PyYAML loads it as `True`) has only `workflow_dispatch`, inputs `tag` and `package` required, `request_id` not required. Top-level `permissions == {}`, the job's permissions `== {'contents': 'write', 'id-token': 'write'}`.
- `test_template_never_clobbers`: `'--clobber' not in text`.
- `test_template_pins_actions_by_sha`: every `uses:` value matches `^[\w.-]+/[\w.-]+@[0-9a-f]{40}$`.
- `test_template_inputs_only_via_env`: no `${{ inputs.` inside any `run:` block.
- `test_actionlint_clean`: `actionlint templates/sign.yml` exits 0, skipped when actionlint is not on PATH.
- Plan step tests, with the heredoc extracted from the template and run as `python3 -I -` with `file://` URLs for a fake PyPI JSON and two fake files:
  - `test_plan_downloads_and_lists`: release with no assets gives `to_sign` and `to_upload` both equal to the two names.
  - `test_plan_skips_bundled_and_present`: assets contain `a.whl` (same bytes in `EXISTING_DIR`) and `a.whl.sigstore.json` gives `to_sign=b.tar.gz`, `to_upload=b.tar.gz`.
  - `test_plan_fails_on_pypi_hash_mismatch`: exit 1, stderr names the file.
  - `test_plan_fails_on_release_asset_mismatch`: different bytes in `EXISTING_DIR` give exit 1 naming the asset.
  - `test_plan_fails_without_files`: empty `urls` gives exit 1.
- `test_input_validation_rejects_injection`: run the validation step's script with `TAG='v1;touch pwned'`, expect exit 1 and no `pwned` file.

- [ ] **Step 2: Run, expect FAIL (template missing)**

- [ ] **Step 3: Write `templates/sign.yml`**

`timeout-minutes: 30`, `runs-on: ubuntu-latest`, `GH_TOKEN: ${{ github.token }}`, `GH_REPO: ${{ github.repository }}`, inputs mapped into `env:`.

- [ ] **Step 4: Run the gate and `actionlint templates/sign.yml`, expect PASS, commit**

```bash
git add templates/sign.yml tests/test_sign_template.py pyproject.toml uv.lock
git commit -m "feat: sign.yml template that signs PyPI files with the workflow identity"
```

---

### Task 3: Dispatch and watch sign.yml

**Files:**
- Create: `python_project_tools/ci_sign.py`
- Modify: `python_project_tools/github.py` (add `created_at: str` to `Release`, filled from `created_at` in `_release`, default `''` so existing fakes keep working)
- Test: `tests/test_ci_sign.py`

**Interfaces:**
- Consumes: `common.run`, `signing.SigningError`.
- Produces:
  - `WORKFLOW: str = 'sign.yml'`, `WORKFLOW_PATH: str = '.github/workflows/sign.yml'`, `FIND_SECONDS: float = 120`, `POLL_SECONDS: float = 3`
  - `@dataclasses.dataclass(frozen=True) class Run: id: int, url: str`
  - `default_branch(repo: str) -> str`
  - `workflow_since(repo: str, branch: str) -> dt.date | None`: date of the oldest commit touching `WORKFLOW_PATH` on `branch` (`gh api repos/{repo}/commits?path=...&sha={branch}` paginated), `None` when there is none, which means sign.yml is absent.
  - `dispatch(repo: str, tag: str, package: str, branch: str) -> Run`: `request_id = uuid.uuid4().hex`, `gh workflow run sign.yml -R repo --ref branch -f tag=... -f package=... -f request_id=...`, then polls `gh run list -R repo --workflow sign.yml --event workflow_dispatch --limit 20 --json databaseId,displayTitle,url` until a run whose `displayTitle` ends with `request_id` appears. Raises `SigningError` after `FIND_SECONDS`.
  - `watch(repo: str, run: Run) -> None`: `gh run watch <id> -R repo --exit-status`, with `capture=False`. Non-zero raises `SigningError(f'sign.yml run failed: {run.url}')`.

- [ ] **Step 1: Write the failing tests** (Recorder, `time.sleep` and `time.monotonic` monkeypatched)

- `test_dispatch_finds_own_run_among_others`: `gh run list` returns two runs, `Sign v1.0 <other>` and `Sign v1.0 <ours>`, with `uuid.uuid4` patched. Returns the second `Run`.
- `test_dispatch_polls_until_run_appears`: first list empty, second has it. Two list calls.
- `test_dispatch_gives_up`: never appears, raises `SigningError` mentioning `sign.yml`.
- `test_watch_failure_names_url`: returncode 1 gives a `SigningError` containing the URL.
- `test_workflow_since_absent`: empty commit list gives `None`.
- `test_workflow_since_oldest`: two commits give the older date.
- `test_release_created_at_parsed` in `tests/test_github.py`.

- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run the gate, expect PASS, commit**

```bash
git add python_project_tools/ci_sign.py python_project_tools/github.py tests/test_ci_sign.py tests/test_github.py
git commit -m "feat: dispatch sign.yml and follow its run"
```

---

### Task 4: ppt sign takes its Sigstore bundles from CI

**Files:**
- Modify: `python_project_tools/sign.py`
- Test: `tests/test_sign.py`

**Interfaces:**
- Consumes: `identities.verify_bundles`, `identities.BundleCheck`, `ci_sign.default_branch`, `ci_sign.workflow_since`, `ci_sign.dispatch`, `ci_sign.watch`, `github.Release.created_at`.
- Produces:
  - `SigstoreMode = typing.Literal['ci', 'browser', 'off']`. `SignOptions.sigstore: bool` becomes `SignOptions.sigstore: SigstoreMode = 'ci'`. The backfill callers (`backfill.py:1356`, `backfill_batch.py:151`) map their boolean to `'browser'` or `'off'`, so the backfill behaves as before.
  - `EXIT_NO_WORKFLOW: int = 3`.
  - `class NoWorkflowError(RuntimeError)`, raised before anything is uploaded when the mode is `'ci'` and `workflow_since` is `None`, with the message `no sign.yml on {branch} of {repo}: copy templates/sign.yml from python-project-tools, or pass --sigstore-browser`.
  - CLI: `--sigstore-browser` (with the default or with `--sigstore-only`, refused with `--gpg-only`).

Flow in `_sign_files` for mode `'ci'`, before `_make_signatures`:
1. `BundleCheck` per dist, with the bundle asset downloaded into `release_dir / 'bundles'` when present.
2. `verify_bundles`. A present bundle with verdict `None` fails the outcome: `invalid bundle on the release: <name>`.
3. Missing bundles: when the release `created_at` date is before `workflow_since`, or any file is rebuilt (`pathlib.Path` in `files`), fail with `predates sign.yml, sign with --sigstore-browser` and dispatch nothing. Otherwise `dispatch`, `watch`, re-read the release with `github.releases`, and repeat steps 1 and 2 once. Still missing fails with `sign.yml ran but left no bundle for <names>`.
4. Continue with GPG and the `SHA256SUMS` pair, skipping `signing.sigstore_sign`.

`main` checks the workflow once per run, before the first release, so a missing sign.yml exits 3 with nothing uploaded. `_finish`, footer and `--dry-run` keep their behaviour. In dry run, mode `'ci'` prints `would dispatch sign.yml for <names>` instead of signing.

- [ ] **Step 1: Write the failing tests** (Recorder plus monkeypatched `identities.verify_bundles` and `ci_sign` functions, the style `test_sign.py` already uses)

- `test_ci_bundles_present_no_dispatch`: all verdicts `'ci'`. No dispatch, `.asc` and the `SHA256SUMS` pair uploaded, status `signed`.
- `test_ci_missing_dispatches_then_signs`: first verdicts missing, after the dispatch all `'ci'`. Exactly one `dispatch` and one `watch`, then GPG.
- `test_ci_invalid_bundle_stops`: a present bundle with verdict `None` fails naming it, with no dispatch and no upload.
- `test_ci_run_failure_reports_url`: `watch` raises, so the outcome fails with the URL and nothing is uploaded.
- `test_no_workflow_exits_3_before_upload`: `workflow_since` returns `None`. `main([...])` returns 3, the Recorder saw no `gh release upload`, stderr names `templates/sign.yml`.
- `test_all_skips_releases_before_cutover`: two releases missing bundles, `created_at` before and after `workflow_since`. Only the newer dispatches, the older fails with `predates sign.yml`.
- `test_rebuilt_files_not_dispatched`.
- `test_rick_bundles_accepted`: verdicts `'rick'` count as present.
- `test_sigstore_browser_flag_uses_browser_signer`: `--sigstore-browser` calls `signing.sigstore_sign` and never `ci_sign`.
- `test_gpg_only_with_sigstore_browser_refused`: exit 2.
- Existing backfill tests stay green with `'browser'`.

- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run the gate, expect PASS, commit**

```bash
git add python_project_tools/sign.py python_project_tools/backfill.py python_project_tools/backfill_batch.py tests/test_sign.py
git commit -m "feat: ppt sign gets Sigstore bundles from sign.yml instead of a browser login"
```

---

### Task 5: verify-release checks the bundle identities

**Files:**
- Modify: `python_project_tools/verify_release.py` (`check_signatures`)
- Test: `tests/test_verify_release.py`

**Interfaces:**
- Consumes: `identities.verify_bundles`, `signing.parse_sha256sums`, `github.download_asset`.
- `check_signatures(tag: str) -> common.Result` keeps its signature. After the presence check it downloads `SHA256SUMS` and the bundles into a temporary directory and builds a `BundleCheck` per dist from the sha256 in `SHA256SUMS`. A dist missing from `SHA256SUMS` or a verdict `None` gives FAIL naming the file. PASS detail: `N files with .asc and .sigstore.json (ci: X, rick: Y), SHA256SUMS signed`.

Verifying by digest keeps the dists themselves off the download list. `SHA256SUMS.asc` is signed by Rick's key, so a digest taken from it is one Rick vouched for.

- [ ] **Step 1: Write the failing tests**: `test_signatures_identity_pass` with mixed `ci` and `rick` verdicts, `test_signatures_identity_fail_names_file`, `test_signatures_dist_missing_from_sums`.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run the gate, expect PASS, commit**

```bash
git add python_project_tools/verify_release.py tests/test_verify_release.py
git commit -m "feat: verify-release checks who signed each Sigstore bundle"
```

---

### Task 6: Documentation and the release skill

**Files:**
- Modify: `VERIFY.md` (replace the section "Checking a bundle signed by CI")
- Modify: `README.md` (the `ppt sign` usage, if it mentions the browser login)
- Modify: `CHANGELOG.rst` (new `2.2.0:` entry), `pyproject.toml` (version `2.2.0`)
- Modify: `~/workspace/ai/skills/release-python-package/SKILL.md` step 3b (separate repository, own branch `ppt-sign-ci` and commit)

Content, from the spec:
- VERIFY.md CI section: the command

  ```bash
  uvx sigstore verify github --bundle FILE.sigstore.json --repository WoLpH/<repo> --trigger workflow_dispatch --name 'Sign release' FILE
  ```

  plus one sentence that the certificate names `sign.yml` on a branch of that repository, unlike PyPI's attestation, which names publish.yml. Include the `--cert-identity` form for a reader who wants the exact branch. Follow the punctuation and spelling rules, and keep the register of the existing page.
- SKILL.md step 3b: `ppt sign vX.Y.Z`, no browser. When it exits 3 (no sign.yml in the repository yet), run `ppt sign vX.Y.Z --sigstore-browser` and say so in the report. Remove the sentence "Once CI signs with Sigstore (plan 3), this becomes `ppt sign vX.Y.Z --gpg-only`."

- [ ] **Step 1: Write the changes**
- [ ] **Step 2: Check the sign.yml `name:` matches the `--name` in VERIFY.md** (`Sign release`)
- [ ] **Step 3: Scan the changed text files**: `LC_ALL=C grep -nP '[^\x00-\x7F]'` and `grep -n ';'` on the prose lines, both empty.
- [ ] **Step 4: Commit in both repositories**

```bash
git add VERIFY.md README.md CHANGELOG.rst pyproject.toml uv.lock
git commit -m "docs: verifying CI-signed bundles, ppt sign without a browser"
```

---

### Task 7: Pilot on python-utils

Steps outside this repository with outward effects, each one confirmed with Rick before it runs.

- [ ] **Step 1:** `uv tool install --editable ~/workspace/python-project-tools --force` and `ppt sign --help` shows `--sigstore-browser`.
- [ ] **Step 2:** In `~/workspace/python-utils`, branch `sign-workflow` from `origin/develop`, copy `templates/sign.yml` to `.github/workflows/sign.yml`, run `actionlint`, commit, push, open the PR, watch CI to green. Merge only when Rick asks.
- [ ] **Step 3: Smoke run on an existing release:** `gh workflow run sign.yml -R WoLpH/python-utils --ref develop -f tag=v4.2.1 -f package=python-utils`. Expected: green, `to_sign` empty (the bundles are already there), nothing uploaded. That proves the trigger, the permissions, the PyPI download and the hash checks.
- [ ] **Step 4:** Merge WoLpH/python-utils#78 through the merge-prs skill (security read, compat check), as Rick asked.
- [ ] **Step 5:** Release through the release-python-package skill. Step 3b runs `ppt sign vX.Y.Z`. Expected: a dispatch, a green sign.yml run, GPG files, no browser.
- [ ] **Step 6:** `ppt verify-release --version X.Y.Z --wait 900` passes with `signatures` at `ci: N, rick: 0`, and the `uvx sigstore verify github` command from VERIFY.md passes on one wheel by hand.

### Task 8: Rollout to the other repositories

Only after Task 7 is green. One PR per repository of decision 10 that has workflows (`gh repo view` plus `.github/workflows` on the default branch), adding the unchanged template. Each PR is watched to green and merged only when Rick asks. speedups gets the same file and nothing else. The list of repositories and PR URLs goes into `docs/superpowers/plans/2026-11-13-ci-sigstore-signing-ledger.md`.
