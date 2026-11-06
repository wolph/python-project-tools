# Backfill tooling: inventory, match-tag, sign, backfill (plan 2 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `ppt` the commands that put every existing PyPI version of the
22 WoLpH packages on a GitHub release with the exact PyPI files, a GPG
signature and a Sigstore bundle per file and a signed SHA256SUMS, rerunnable
until nothing is left to do.

**Architecture:** Thin clients for PyPI (`pypi.py`) and GitHub through `gh`
(`github.py`) sit on a shared `common.py` (Result, run, fetch_json, gh_json,
sha256). Pure logic lives in `versions.py` (tag normalisation, PEP 440) and
`signing.py` (gpg, sigstore, SHA256SUMS). The commands `inventory`, `sign`,
`match-tag` and `backfill` compose those. Every network and subprocess call
goes through `common.run` or `common.fetch_json`, so tests replace exactly
those two seams with recorders and fakes.

**Tech Stack:** Python >=3.10, argparse, packaging, gpg (binary), `sigstore`
4.x via `uvx sigstore`, `gh` CLI, `uv build` for rebuilds, pytest.

**Spec:** `docs/superpowers/specs/2026-11-06-signed-releases-design.md`,
sections "2. CLI surface", "3. Backfill procedure per package", "5.
VERIFY.md", the calls list, and "Prerequisites that only Rick can do".
Plan 1 (`2026-11-06-revival-and-script-migration.md`) built the package this
extends. Plan 3 covers publish.yml and the CI side.

## Global Constraints

- Python floor 3.10. Runtime deps stay `packaging` and the `tomli` marker.
  No HTTP library, `urllib` through `common.fetch_json` and `common.fetch_bytes`.
- Every subprocess goes through `common.run(args, check=False, cwd=None)`.
  Every HTTP GET goes through `common.fetch_json` or `common.fetch_bytes`.
  Tests monkeypatch those and nothing else.
- GPG key default `149325FD15904E9C4EB89E95E81444E9CE1F695D`
  (`signing.DEFAULT_GPG_KEY`), overridable with `--gpg-key`. The private key
  never leaves the machine, signing shells out to `gpg`.
- Sigstore signs with `uvx sigstore sign --output-directory DIR FILE...`,
  interactive OIDC. Bundles are `<file>.sigstore.json`.
- Signature file names: `<file>.asc` (ASCII armoured detached),
  `<file>.sigstore.json`, `SHA256SUMS`, `SHA256SUMS.asc`. Nothing else.
- Every release created by this code passes `--latest=false` and
  `--verify-tag`. Pre-releases per PEP 440 pass `--prerelease`.
- Existing tags are never moved or deleted. New tags are annotated and
  signed (`git tag -s`), style `v<version>`.
- Nothing on PyPI is changed. Downloads are verified against the PyPI
  sha256 before any signature is made. A mismatch aborts that version.
- Exit statuses: 0 done, 1 at least one version failed or was skipped with a
  reason, 2 the command refused to run (precondition, see each command).
- Repository override table for PyPI project URLs that point at sponsors:
  `{'numpy-stl': 'WoLpH/numpy-stl', 'portalocker': 'WoLpH/portalocker', 'progressbar2': 'WoLpH/python-progressbar'}`.
- Style: ruff.toml, `ty check` clean, explicit type annotations, tests under
  `tests/`, prose in ASCII punctuation, no semicolons, British spelling.
  Suppression comments name their rule. New code adds no `noqa: E501`:
  wrap instead.
- Git: feature branch in a worktree, signed commits, invoke git as
  `/usr/bin/git` in this environment (rtk hook), push to master directly
  as Rick ruled for this repository, after the full gate is green.

## Review Focus

1. A downloaded file whose sha256 differs from PyPI's is never signed and
   the version is reported as failed. Test in Task 2.
2. An existing release asset with the same name and different bytes is
   left alone and reported, unless `--replace-assets`. Test in Task 7.
3. `backfill` refuses (exit 2) when the repository's default branch has an
   enabled workflow with a `release:` trigger, before creating anything.
   Test in Task 9.
4. A backfilled release never becomes "Latest": every create passes
   `--latest=false` and `backfill` asserts the latest release afterwards is
   the highest stable version it saw before. Tests in Tasks 3 and 9.
5. A second `sign` run on an already signed release uploads nothing and does
   not append a second footer. Test in Task 7.

---

### Task 1: `common.py`, the shared seam

**Files:**
- Create: `python_project_tools/common.py`
- Modify: `python_project_tools/verify_release.py` (drop its `Result`,
  `run`, `fetch_json`, `read_package_name`, import from common),
  `python_project_tools/conda_forge.py` (drop its `Result`, `run`,
  `gh_json`, `fetch_json`, import from common, convert every positional
  `Result(status, name, evidence)` call to keyword arguments)
- Test: `tests/test_common.py`, adjust `tests/test_verify_release.py` and
  `tests/test_conda_forge.py` monkeypatch targets to `common.*`

**Interfaces:**
- Produces:
  ```python
  @dataclasses.dataclass
  class Result:
      name: str
      status: str  # PASS, FAIL or SKIP
      evidence: str

  class FetchError(Exception): ...  # network or decode failure, carries url

  def run(args: list[str], check: bool = False, cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess[str]
  def gh_json(args: list[str]) -> object | None
  def fetch_json(url: str) -> object | None          # None only on HTTP 404
  def fetch_bytes(url: str, into: pathlib.Path) -> pathlib.Path  # raises FetchError
  def sha256_of(path: pathlib.Path) -> str
  def read_package_name(root: pathlib.Path = pathlib.Path('.')) -> str
  ```
  `fetch_json` raises `FetchError` on anything other than success or 404,
  so an outage no longer reads as "not found".

- [ ] **Step 1: Write the failing tests**

`tests/test_common.py`:

```python
def test_sha256_of_matches_hashlib(tmp_path: pathlib.Path) -> None:
    path = tmp_path / 'spam.txt'
    path.write_bytes(b'spam and eggs')
    assert common.sha256_of(path) == hashlib.sha256(b'spam and eggs').hexdigest()


def test_fetch_json_returns_none_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, 'urlopen', _raising(urllib.error.HTTPError(url='u', code=404, msg='', hdrs=None, fp=None)))
    assert common.fetch_json('https://example.invalid/x') is None


def test_fetch_json_raises_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, 'urlopen', _raising(urllib.error.URLError('down')))
    with pytest.raises(common.FetchError):
        common.fetch_json('https://example.invalid/x')


def test_fetch_bytes_writes_the_body(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, 'urlopen', _responding(b'wheel bytes'))
    out = common.fetch_bytes('https://example.invalid/w.whl', tmp_path / 'w.whl')
    assert out.read_bytes() == b'wheel bytes'


def test_results_are_keyword_constructible() -> None:
    assert common.Result(name='tag', status='PASS', evidence='ok').status == 'PASS'
```

`_raising` and `_responding` are two tiny helpers in the test module that
return a callable standing in for `urllib.request.urlopen` (a context
manager whose `read` returns the bytes). `common` exposes `urlopen` as a
module attribute (`urlopen = urllib.request.urlopen`) so tests can replace
it.

- [ ] **Step 2: Run the tests to see them fail**

Run: `uv run pytest tests/test_common.py -v`
Expected: FAIL, `python_project_tools.common` does not exist.

- [ ] **Step 3: Implement `common.py` and rewire the two modules**

`fetch_json`: HTTP 404 returns None, every other `URLError`,
`HTTPError`, `TimeoutError` or `json.JSONDecodeError` raises `FetchError`
with the url in the message. `conda_forge.check_conda`-style callers that
used None for "anything went wrong" now catch `FetchError` and report
`FAIL` with the error text, not SKIP. In `conda_forge.py`, replace each
`Result(<status>, <name>, <evidence>)` by keyword arguments. Delete the
three inline package-name reads and call `common.read_package_name`.

- [ ] **Step 4: Run the whole suite, ruff and ty**

Run: `uv run pytest -v && uv run ruff check --fix && uv run ruff format && uv run ty check`
Expected: all pass. The conda_forge and verify_release tests still pass
with their monkeypatch targets moved to `common`.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add python_project_tools tests
/usr/bin/git commit -S -m 'refactor: one common module for Result, run, fetch and gh helpers'
```

### Task 2: `pypi.py`, the PyPI client

**Files:**
- Create: `python_project_tools/pypi.py`
- Test: `tests/test_pypi.py`

**Interfaces:**
- Consumes: `common.fetch_json`, `common.fetch_bytes`, `common.sha256_of`.
- Produces:
  ```python
  @dataclasses.dataclass(frozen=True)
  class PypiFile:
      filename: str
      url: str
      sha256: str
      size: int
      yanked: bool
      upload_time: datetime.datetime  # timezone aware, UTC
      packagetype: str  # 'sdist' or 'bdist_wheel'

  @dataclasses.dataclass(frozen=True)
  class Project:
      name: str
      latest: str
      releases: dict[str, list[PypiFile]]  # every version, empty list allowed
      repo: str | None  # 'Owner/name' from project URLs, override table applied

  REPO_OVERRIDES: dict[str, str]
  class IntegrityError(Exception): ...

  def project(package: str) -> Project          # raises FetchError, LookupError on 404
  def repo_from(info: dict[str, object], package: str) -> str | None
  def download(file: PypiFile, into: pathlib.Path) -> pathlib.Path  # verifies sha256, raises IntegrityError
  def files_for(project: Project, version: str) -> list[PypiFile]
  ```

- [ ] **Step 1: Write the failing tests**

`tests/test_pypi.py`, with a `FAKE_JSON` dict shaped like PyPI's
`/pypi/<name>/json` holding two versions, one yanked file, project_urls with
`https://github.com/WoLpH/spam-eggs/`:

```python
def test_project_parses_files_and_repo(monkeypatch) -> None:
    monkeypatch.setattr(common, 'fetch_json', lambda url: FAKE_JSON)
    proj = pypi.project('spam-eggs')
    assert proj.repo == 'WoLpH/spam-eggs'
    assert sorted(proj.releases) == ['1.0', '1.1']
    assert proj.releases['1.1'][0].yanked is True
    assert proj.releases['1.0'][0].upload_time.tzinfo is not None


def test_repo_override_wins_over_sponsors_url() -> None:
    info = {'home_page': 'https://github.com/sponsors/wolph', 'project_urls': {}}
    assert pypi.repo_from(info, 'portalocker') == 'WoLpH/portalocker'


def test_repo_from_handles_dot_git_suffix() -> None:
    info = {'project_urls': {'Source': 'https://github.com/WoLpH/mt940.git'}}
    assert pypi.repo_from(info, 'mt-940') == 'WoLpH/mt940'


def test_project_raises_lookup_error_on_404(monkeypatch) -> None:
    monkeypatch.setattr(common, 'fetch_json', lambda url: None)
    with pytest.raises(LookupError):
        pypi.project('nope')


def test_download_verifies_sha256(tmp_path, monkeypatch) -> None:
    body = b'sdist bytes'
    monkeypatch.setattr(common, 'fetch_bytes', _writing(body))
    good = _file(sha256=hashlib.sha256(body).hexdigest())
    assert pypi.download(good, tmp_path).read_bytes() == body


def test_download_rejects_sha256_mismatch(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(common, 'fetch_bytes', _writing(b'tampered'))
    with pytest.raises(pypi.IntegrityError):
        pypi.download(_file(sha256='0' * 64), tmp_path)
    assert list(tmp_path.iterdir()) == []  # the bad file is removed
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/test_pypi.py -v`
Expected: FAIL, module missing.

- [ ] **Step 3: Implement `pypi.py`**

`project` fetches `https://pypi.org/pypi/{package}/json`. `upload_time`
from `upload_time_iso_8601`. `repo_from` scans `home_page` and every
`project_urls` value with `github\.com/([\w.-]+)/([\w.-]+)`, strips `.git`,
skips `sponsors/`, applies `REPO_OVERRIDES` first. `download` writes to
`into / file.filename`, compares `sha256_of`, unlinks and raises on
mismatch.

- [ ] **Step 4: Run tests, ruff, ty**

Run: `uv run pytest tests/test_pypi.py -v && uv run ruff check --fix && uv run ruff format && uv run ty check`
Expected: 6 passed, clean.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add python_project_tools/pypi.py tests/test_pypi.py
/usr/bin/git commit -S -m 'feat: PyPI client with sha256-verified downloads'
```

### Task 3: `github.py`, releases and tags through `gh`

**Files:**
- Create: `python_project_tools/github.py`
- Test: `tests/test_github.py`, `tests/fakes.py` (the recorder)

**Interfaces:**
- Consumes: `common.run`, `common.gh_json`.
- Produces:
  ```python
  @dataclasses.dataclass(frozen=True)
  class Tag:
      name: str
      sha: str

  @dataclasses.dataclass(frozen=True)
  class Asset:
      name: str
      size: int
      url: str  # browser_download_url

  @dataclasses.dataclass
  class Release:
      tag: str
      id: int
      body: str
      prerelease: bool
      draft: bool
      assets: list[Asset]

  def tags(repo: str) -> list[Tag]
  def releases(repo: str) -> list[Release]
  def latest_release(repo: str) -> Release | None
  def create_release(repo: str, tag: str, title: str, notes: str, prerelease: bool) -> Release
  def upload_assets(repo: str, tag: str, paths: list[pathlib.Path], clobber: bool = False) -> None
  def edit_notes(repo: str, tag: str, body: str) -> None
  def download_asset(asset: Asset, into: pathlib.Path) -> pathlib.Path
  def release_triggered_workflows(repo: str) -> list[str]  # active workflows whose file has an `on.release` trigger
  def default_branch(repo: str) -> str
  ```
  `create_release` always runs
  `gh release create TAG --repo REPO --title TITLE --notes-file FILE --verify-tag --latest=false [--prerelease]`.

`tests/fakes.py`:

```python
class Recorder:
    """Stand-in for common.run that replays canned results and records calls."""

    def __init__(self, replies: dict[str, subprocess.CompletedProcess[str]]) -> None: ...
    def __call__(self, args: list[str], check: bool = False, cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess[str]: ...
    calls: list[list[str]]
```

Keys of `replies` are the joined argv prefix up to the first argument that
starts with `--notes-file`, so paths in temp dirs do not break matching.
`__call__` picks the longest key that is a prefix of the joined argv, and
raises `AssertionError` listing the argv when nothing matches. Later tasks
reuse it.

- [ ] **Step 1: Write the failing tests**

```python
def test_tags_paginates_and_parses(monkeypatch) -> None: ...  # two pages via gh api --paginate replay
def test_create_release_passes_latest_false_and_verify_tag(monkeypatch) -> None:
    rec = Recorder({...})
    monkeypatch.setattr(common, 'run', rec)
    github.create_release('WoLpH/spam', 'v1.0', 'v1.0', 'notes', prerelease=False)
    argv = rec.calls[0]
    assert '--latest=false' in argv and '--verify-tag' in argv and '--prerelease' not in argv
def test_create_release_marks_prerelease(monkeypatch) -> None: ...
def test_release_triggered_workflows_reads_active_files(monkeypatch) -> None:
    # workflows list reply: two workflows, one state 'active' with `on: release:` content, one disabled
    assert github.release_triggered_workflows('WoLpH/spam') == ['release.yml']
def test_upload_assets_clobber_flag(monkeypatch) -> None: ...
def test_edit_notes_uses_notes_file(monkeypatch) -> None: ...
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/test_github.py -v`
Expected: FAIL, module missing.

- [ ] **Step 3: Implement `github.py` and `tests/fakes.py`**

`tags` and `releases` use `gh api --paginate repos/{repo}/tags?per_page=100`
and `repos/{repo}/releases?per_page=100` through `gh_json` (the paginated
output is a stream of arrays, concatenate them). `release_triggered_workflows`
lists `repos/{repo}/actions/workflows`, keeps `state == 'active'`, fetches
each `path` via `repos/{repo}/contents/{path}`, base64-decodes and matches
`^\s*release:\s*$` or `^\s*release:\s*\n?\s*types:` with `re.MULTILINE`.

- [ ] **Step 4: Run tests, ruff, ty**

Run: `uv run pytest tests/test_github.py -v && uv run ruff check --fix && uv run ruff format && uv run ty check`
Expected: passed, clean.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add python_project_tools/github.py tests/test_github.py tests/fakes.py
/usr/bin/git commit -S -m 'feat: GitHub releases and tags through gh, with a replaying recorder for tests'
```

### Task 4: `versions.py`, tags and PEP 440

**Files:**
- Create: `python_project_tools/versions.py`
- Test: `tests/test_versions.py`

**Interfaces:**
- Produces:
  ```python
  TAG_PREFIXES: tuple[str, ...] = ('release-', 'release_', 'rel-', 'version-', 'v')
  def normalise(tag_or_version: str) -> str      # lower, strip one prefix, '_' and '-' to '.'
  def is_version_tag(tag: str) -> bool            # normalised form parses as packaging Version
  def match_tag(version: str, tags: list[github.Tag]) -> github.Tag | None
  def is_prerelease(version: str) -> bool         # packaging Version.is_prerelease
  def tag_for(version: str) -> str                # f'v{version}'
  def sort_key(version: str) -> packaging.version.Version
  ```

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize(('tag', 'expected'), [
    ('v1.2.3', '1.2.3'), ('1.2.3', '1.2.3'), ('release-1_2', '1.2'),
    ('vv2.0.0', 'v2.0.0'), ('untagged-7f3a', 'untagged.7f3a'),
])
def test_normalise(tag, expected) -> None: ...

@pytest.mark.parametrize(('tag', 'ok'), [('v1.0', True), ('ci', False), ('vv2.0.0', False), ('untagged-7f3a', False), ('4.17.0', True)])
def test_is_version_tag(tag, ok) -> None: ...

def test_match_tag_prefers_exact_then_normalised() -> None:
    tags = [Tag('1.0', 'a'), Tag('v1.0', 'b')]
    assert versions.match_tag('1.0', tags) == Tag('1.0', 'a')
    assert versions.match_tag('2.0', tags) is None

@pytest.mark.parametrize(('v', 'pre'), [('1.0', False), ('1.0rc1', True), ('1.0.dev3', True), ('1.0.post1', False)])
def test_is_prerelease(v, pre) -> None: ...
```

- [ ] **Step 2: Run to see them fail**, **Step 3: Implement**, **Step 4: Run tests, ruff, ty**

Run: `uv run pytest tests/test_versions.py -v && uv run ruff check --fix && uv run ruff format && uv run ty check`
Expected: passed, clean.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add python_project_tools/versions.py tests/test_versions.py
/usr/bin/git commit -S -m 'feat: tag normalisation and PEP 440 helpers'
```

### Task 5: `ppt inventory`

**Files:**
- Create: `python_project_tools/inventory.py`
- Modify: `python_project_tools/cli.py` (register `inventory`), `README.md`
  (Commands section)
- Test: `tests/test_inventory.py`

**Interfaces:**
- Consumes: `pypi.project`, `github.tags`, `github.releases`, `versions.*`.
- Produces:
  ```python
  @dataclasses.dataclass
  class VersionRow:
      version: str
      files: int
      yanked: bool
      tag: str | None
      release: bool
      dist_assets: int     # release assets whose name is one of the PyPI filenames
      asc_assets: int      # '<pyPI filename>.asc' present
      sigstore_assets: int # '<filename>.sigstore.json' present
      sha256sums: bool
      note: str            # 'untagged', 'empty on pypi', 'rebuilt', ''

  @dataclasses.dataclass
  class PackageInventory:
      package: str
      repo: str | None
      rows: list[VersionRow]
      version_tags_without_release: list[str]   # version-shaped tags with no PyPI version
      odd_tags: list[str]                       # tags that are not version-shaped

  def inventory(package: str, repo: str | None = None) -> PackageInventory
  def to_markdown(inv: PackageInventory) -> str
  def to_json(invs: list[PackageInventory]) -> str
  def main(argv: list[str]) -> int   # ppt inventory PACKAGE... [--repo OWNER/NAME] [--json]
  ```

- [ ] **Step 1: Write the failing tests**

With `pypi.project`, `github.tags` and `github.releases` monkeypatched to
return fixtures (three versions: one tagged and released with all four
asset kinds, one tagged without release, one untagged, plus one `ci` tag and
one `v9.9` tag without a PyPI version):

```python
def test_inventory_classifies_every_version(monkeypatch) -> None:
    inv = inventory.inventory('spam-eggs')
    by = {r.version: r for r in inv.rows}
    assert by['1.0'].tag == 'v1.0' and by['1.0'].release and by['1.0'].asc_assets == 2
    assert by['1.1'].tag == 'v1.1' and not by['1.1'].release
    assert by['1.2'].tag is None and by['1.2'].note == 'untagged'
    assert inv.version_tags_without_release == ['v9.9']
    assert inv.odd_tags == ['ci']

def test_markdown_has_one_row_per_version(monkeypatch) -> None: ...
def test_json_round_trips(monkeypatch) -> None: ...
def test_main_exits_2_without_repo(monkeypatch) -> None:  # project with repo None and no --repo
def test_inventory_is_registered() -> None:
    assert cli.COMMANDS['inventory'] is inventory.main
```

- [ ] **Step 2: Run to see them fail**, **Step 3: Implement**

Markdown table columns in this order: version, files, yanked, tag,
release, dist, asc, sigstore, sums, note. A trailing paragraph lists
`version_tags_without_release` and `odd_tags`. `main` prints one table per
package, or one JSON document with `--json`, and exits 0.

- [ ] **Step 4: Run tests, ruff, ty**

Run: `uv run pytest -v && uv run ruff check --fix && uv run ruff format && uv run ty check`
Expected: passed, clean.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add python_project_tools tests README.md
/usr/bin/git commit -S -m 'feat: ppt inventory, PyPI versions against tags, releases and signatures'
```

### Task 6: `signing.py`, gpg, sigstore and SHA256SUMS

**Files:**
- Create: `python_project_tools/signing.py`
- Test: `tests/test_signing.py`

**Interfaces:**
- Consumes: `common.run`, `common.sha256_of`.
- Produces:
  ```python
  DEFAULT_GPG_KEY: str = '149325FD15904E9C4EB89E95E81444E9CE1F695D'
  SIGSTORE_ARGV: list[str] = ['uvx', 'sigstore']

  def gpg_sign(path: pathlib.Path, key: str = DEFAULT_GPG_KEY) -> pathlib.Path
      # gpg --batch --yes --armor --detach-sign --local-user KEY --output PATH.asc PATH
  def gpg_verify(path: pathlib.Path, signature: pathlib.Path) -> bool
  def sigstore_sign(paths: list[pathlib.Path]) -> list[pathlib.Path]
      # one invocation: SIGSTORE_ARGV + ['sign', '--output-directory', str(paths[0].parent), '--overwrite', *paths]
      # returns the .sigstore.json paths, raises SigningError when any is missing afterwards
  def write_sha256sums(directory: pathlib.Path, files: list[pathlib.Path], rebuilt: dict[str, str]) -> pathlib.Path
      # rebuilt maps filename to the comment line to place above it
  def parse_sha256sums(path: pathlib.Path) -> dict[str, str]  # filename -> sha256, comments ignored
  class SigningError(Exception): ...
  ```

- [ ] **Step 1: Write the failing tests**

gpg tests use a throwaway `GNUPGHOME` under `tmp_path` with a key made by
`gpg --batch --quick-generate-key 'ppt test <ppt@example.invalid>' ed25519 sign 1d`
(the key id read back with `--list-secret-keys --with-colons`), and skip
with `pytest.importorskip`-style `shutil.which('gpg') is None`. The real
`gpg` binary runs, `common.run` is not patched here.

```python
def test_gpg_sign_and_verify_round_trip(gnupg_home, tmp_path) -> None: ...
def test_gpg_verify_fails_on_changed_file(gnupg_home, tmp_path) -> None: ...
def test_sigstore_sign_invokes_once_for_all_files(tmp_path, monkeypatch) -> None:
    rec = Recorder({'uvx sigstore sign': _touching('.sigstore.json')})  # the fake creates the bundle files
    monkeypatch.setattr(common, 'run', rec)
    out = signing.sigstore_sign([tmp_path / 'a.whl', tmp_path / 'a.tar.gz'])
    assert len(rec.calls) == 1 and [p.name for p in out] == ['a.whl.sigstore.json', 'a.tar.gz.sigstore.json']
def test_sigstore_sign_raises_when_bundle_missing(tmp_path, monkeypatch) -> None: ...
def test_sha256sums_format_and_rebuilt_comment(tmp_path) -> None:
    sums = signing.write_sha256sums(tmp_path, [a, b], {'b.tar.gz': '# rebuilt from tag v1.0 on 2026-11-06, not the PyPI bytes'})
    text = sums.read_text()
    assert text.splitlines()[0] == f'{sha_a}  a.whl'
    assert '# rebuilt from tag v1.0' in text and text.index('# rebuilt') < text.index('b.tar.gz')
    assert signing.parse_sha256sums(sums) == {'a.whl': sha_a, 'b.tar.gz': sha_b}
```

- [ ] **Step 2: Run to see them fail**, **Step 3: Implement**, **Step 4: Run tests, ruff, ty**

Run: `uv run pytest tests/test_signing.py -v && uv run ruff check --fix && uv run ruff format && uv run ty check`
Expected: passed (gpg tests run locally, gpg is installed), clean.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add python_project_tools/signing.py tests/test_signing.py
/usr/bin/git commit -S -m 'feat: gpg and sigstore signing plus SHA256SUMS'
```

### Task 7: `ppt sign`

**Files:**
- Create: `python_project_tools/sign.py`
- Modify: `python_project_tools/cli.py`, `README.md`
- Test: `tests/test_sign.py`

**Interfaces:**
- Consumes: `pypi.*`, `github.*`, `signing.*`, `versions.*`.
- Produces:
  ```python
  FOOTER_MARKER: str = '<!-- ppt-signed -->'
  VERIFY_URL: str = 'https://github.com/WoLpH/python-project-tools/blob/master/VERIFY.md'

  @dataclasses.dataclass
  class SignOutcome:
      tag: str
      version: str
      uploaded: list[str]
      kept: list[str]        # same name, same bytes, already there
      conflicts: list[str]   # same name, different bytes, left alone
      status: str            # 'signed', 'already signed', 'failed'
      reason: str

  @dataclasses.dataclass
  class SignOptions:
      repo: str
      package: str
      gpg: bool = True
      sigstore: bool = True
      gpg_key: str = signing.DEFAULT_GPG_KEY
      replace_assets: bool = False
      dry_run: bool = False
      rebuilt: dict[str, str] = dataclasses.field(default_factory=dict)
      signed_on: datetime.date = dataclasses.field(default_factory=datetime.date.today)

  def footer(options: SignOptions, rebuilt_names: list[str]) -> str
  def sign_release(release: github.Release, files: list[PypiFile] | list[pathlib.Path], workdir: pathlib.Path, options: SignOptions) -> SignOutcome
  def releases_missing_signatures(repo: str, project: pypi.Project) -> list[github.Release]
  def main(argv: list[str]) -> int
      # ppt sign TAG | --all  [--repo OWNER/NAME] [--package NAME] [--gpg-only] [--sigstore-only] [--gpg-key ID] [--replace-assets] [--dry-run]
  ```

Footer text, exact (with the date and key substituted, one blank line
before it when the body is not empty):

```
<!-- ppt-signed -->
Signed on {date} with `ppt sign`, after the release. The files above are
the exact PyPI uploads for this version, verified by sha256 against PyPI
before signing. Each file has a GPG signature (`.asc`, key {key}) and a
Sigstore bundle (`.sigstore.json`). `SHA256SUMS.asc` signs the checksum
list. Rebuilt files, if any: {rebuilt or 'none'}. How to verify: {VERIFY_URL}
```

`sign_release` procedure: download each PyPI file into `workdir` (sha256
verified by `pypi.download`), or take the given local paths for rebuilt
files. Compare with existing assets by name: same name and same sha256
(download the asset and hash it) is `kept`, same name and different hash is
`conflicts` unless `replace_assets` (then uploaded with clobber). Produce
`.asc` when `gpg`, `.sigstore.json` when `sigstore`, `SHA256SUMS` always
and `SHA256SUMS.asc` when `gpg`. Upload only files not already present
with identical bytes. Append the footer once (skip when `FOOTER_MARKER`
is in the body). `dry_run` prints every action and performs none.

- [ ] **Step 1: Write the failing tests**

With `common.run` replaced by a `Recorder`, `pypi.download` by a fake that
writes known bytes, `signing.gpg_sign` and `signing.sigstore_sign` by fakes
that touch the output files, and `github.download_asset` by a fake:

```python
def test_sign_release_uploads_everything_on_a_bare_release(...) -> None:
    out = sign.sign_release(bare_release, [whl, sdist], tmp_path, options)
    assert sorted(out.uploaded) == sorted(['s.whl', 's.whl.asc', 's.whl.sigstore.json', 's.tar.gz', 's.tar.gz.asc', 's.tar.gz.sigstore.json', 'SHA256SUMS', 'SHA256SUMS.asc'])
    assert out.status == 'signed'

def test_sign_release_is_idempotent(...) -> None:
    # release already carries every asset with identical bytes and the footer
    out = sign.sign_release(signed_release, [whl, sdist], tmp_path, options)
    assert out.uploaded == [] and out.status == 'already signed'
    assert not any('release edit' in ' '.join(c) for c in rec.calls)  # footer not appended twice

def test_conflicting_asset_is_left_alone_and_reported(...) -> None:
    out = sign.sign_release(release_with_other_bytes, [whl], tmp_path, options)
    assert out.conflicts == ['s.whl'] and 's.whl' not in out.uploaded

def test_replace_assets_clobbers_conflicts(...) -> None: ...
def test_gpg_only_skips_sigstore(...) -> None: ...
def test_footer_names_rebuilt_files(...) -> None: ...
def test_dry_run_performs_nothing(...) -> None:
    assert rec.calls == []
def test_releases_missing_signatures_picks_releases_without_asc(...) -> None: ...
def test_sign_is_registered() -> None: ...
```

- [ ] **Step 2: Run to see them fail**, **Step 3: Implement**, **Step 4: Run tests, ruff, ty**

Run: `uv run pytest -v && uv run ruff check --fix && uv run ruff format && uv run ty check`
Expected: passed, clean.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add python_project_tools tests README.md
/usr/bin/git commit -S -m 'feat: ppt sign, GPG and Sigstore signatures on a GitHub release'
```

### Task 8: `ppt match-tag`

**Files:**
- Create: `python_project_tools/match_tag.py`
- Modify: `python_project_tools/cli.py`, `README.md`
- Test: `tests/test_match_tag.py`, `tests/conftest.py` (a `scratch_repo`
  fixture building a small git repository with three commits bumping a
  version in `setup.py`, each with a matching sdist-like tree)

**Interfaces:**
- Consumes: `common.run`, `pypi.*`, `versions.tag_for`.
- Produces:
  ```python
  VERSION_FILES: tuple[str, ...] = ('setup.py', 'setup.cfg', 'pyproject.toml', '__init__.py', '__about__.py', '__version__.py')
  GENERATED: tuple[str, ...] = ('PKG-INFO', 'setup.cfg', 'MANIFEST', 'MANIFEST.in')  # ignored in the sdist comparison when generated

  @dataclasses.dataclass
  class Candidate:
      sha: str
      committed: datetime.datetime
      matched: int        # tracked files byte-identical to the sdist
      differing: list[str]

  def candidates(checkout: pathlib.Path, version: str) -> list[str]           # git log -S over VERSION_FILES
  def compare(checkout: pathlib.Path, sha: str, sdist: pathlib.Path) -> Candidate
  def choose(cands: list[Candidate], uploaded: datetime.datetime) -> Candidate | None  # only full matches, closest to upload
  def workflow_with_tag_trigger(checkout: pathlib.Path, sha: str) -> list[str]  # .github/workflows/*.yml at sha with `tags:` under on.push
  def create_tag(checkout: pathlib.Path, tag: str, sha: str, message: str) -> None  # git tag -s TAG SHA -m MESSAGE
  def push_tag(checkout: pathlib.Path, tag: str) -> None
  def main(argv: list[str]) -> int
      # ppt match-tag PACKAGE VERSION [--checkout DIR] [--create] [--push] [--allow-workflow-trigger]
  ```
  Tag message, exact:
  `Retroactive tag for PyPI release {package} {version}.\n\nMatched against {sdist filename} (sha256 {sha256}) on {date}: every tracked file in the sdist is byte-identical to this commit.`

- [ ] **Step 1: Write the failing tests**

```python
def test_candidates_finds_the_bump_commit(scratch_repo) -> None: ...
def test_compare_full_match_and_differing(scratch_repo, tmp_path) -> None:
    # build an sdist dir from the second commit's tree, compare against commits 1 to 3
def test_choose_requires_full_match_and_prefers_closest(...) -> None: ...
def test_workflow_with_tag_trigger_detects_publish(scratch_repo) -> None:
    # add .github/workflows/publish.yml with on.push.tags to a commit, expect ['publish.yml'], and [] for a commit without it
def test_main_refuses_push_when_workflow_would_fire(scratch_repo, monkeypatch, capsys) -> None:
    assert match_tag.main([... '--create', '--push']) == 2
    assert 'allow-workflow-trigger' in capsys.readouterr().err
def test_create_tag_is_signed_and_annotated(scratch_repo, gnupg_home) -> None:  # skip without gpg
def test_match_tag_is_registered() -> None: ...
```

- [ ] **Step 2: Run to see them fail**, **Step 3: Implement**

`compare` extracts the sdist to a temp dir, lists its files relative to the
single top-level directory, and for each path that `git ls-tree -r SHA`
tracks, compares `git show SHA:PATH` bytes to the sdist bytes. Files in
`GENERATED` are skipped. `matched` counts identical files, `differing`
lists the rest. A candidate is a full match when `differing` is empty and
`matched > 0`. `main` prints the evidence table (sha, date, matched,
differing) and without `--create` exits 0 on a match, 1 on none.

- [ ] **Step 4: Run tests, ruff, ty**

Run: `uv run pytest -v && uv run ruff check --fix && uv run ruff format && uv run ty check`
Expected: passed, clean.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add python_project_tools tests README.md
/usr/bin/git commit -S -m 'feat: ppt match-tag, find and sign the commit behind an untagged PyPI version'
```

### Task 9: `ppt backfill`

**Files:**
- Create: `python_project_tools/backfill.py`, `python_project_tools/rebuild.py`
- Modify: `python_project_tools/cli.py`, `README.md`
- Test: `tests/test_backfill.py`, `tests/test_rebuild.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  ```python
  # rebuild.py
  def rebuild(checkout: pathlib.Path, tag: str, out: pathlib.Path) -> list[pathlib.Path]
      # git archive TAG | tar -x into a fresh temp dir, then `uv build --out-dir OUT` there, returns the built files, raises RebuildError
  class RebuildError(Exception): ...

  # backfill.py
  UNTAGGED_TAG: str = 'pypi-untagged-releases'
  UNTAGGED_TITLE: str = 'PyPI versions without a matching tag'
  STOCK_NOTES: str = 'Release {version}. No changelog section was found at tag {tag}.'

  @dataclasses.dataclass
  class Plan:   # what one version needs
      version: str
      tag: str | None
      tag_action: str       # 'exists', 'create', 'untagged'
      release_action: str   # 'exists', 'create'
      files_source: str     # 'pypi', 'rebuild', 'none'
      prerelease: bool
      yanked: bool

  @dataclasses.dataclass
  class BackfillReport:
      package: str
      repo: str
      rows: list[tuple[Plan, SignOutcome | None, str]]  # plan, outcome, note
      latest_before: str | None
      latest_after: str | None

  def preflight(repo: str) -> list[str]   # reasons to refuse: enabled release-triggered workflows
  def plan_versions(project: pypi.Project, tags: list[github.Tag], releases: list[github.Release]) -> list[Plan]
  def changelog_section(checkout: pathlib.Path, tag: str, version: str) -> str | None  # same rule as publish.yml: lines from '^{version}:$' to the next '^[0-9][0-9.]*:$'
  def notes_for(plan: Plan, section: str | None) -> str
  def run_backfill(package: str, options: BackfillOptions) -> BackfillReport
  def to_markdown(report: BackfillReport) -> str
  def main(argv: list[str]) -> int
      # ppt backfill PACKAGE [--repo OWNER/NAME] [--checkout DIR] [--only VERSION] [--dry-run] [--replace-assets] [--gpg-only|--sigstore-only] [--allow-workflow-trigger]
  ```

`plan_versions` covers the union the spec asks for: every PyPI version with
files (`files_source 'pypi'`), every PyPI version without files
(`'rebuild'`), and every version-shaped tag that has no PyPI version at all
(`'rebuild'`, `tag_action 'exists'`). Tags that are not version-shaped are
left out.

Order of work inside `run_backfill`: preflight (exit 2 on refusal), record
`latest_before = github.latest_release(repo)`, plan every version oldest
first by `versions.sort_key`, then for each plan: create the tag when
`tag_action == 'create'` (through `match_tag`, refusing to push when a
workflow would fire unless allowed), create the release when missing
(`notes_for`, `--latest=false`, prerelease flag), get files (download or
`rebuild`), `sign.sign_release`. Untagged versions are collected and
handled last on `UNTAGGED_TAG`, created on the root commit
(`git rev-list --max-parents=0 HEAD`) when missing. Rebuild failures are
noted, no release is created for them. Finally assert
`latest_after == latest_before` and print the report.

- [ ] **Step 1: Write the failing tests**

All with fakes and a `Recorder`, no network:

```python
def test_preflight_refuses_enabled_release_workflow(monkeypatch) -> None:
    monkeypatch.setattr(github, 'release_triggered_workflows', lambda repo: ['release.yml'])
    assert backfill.preflight('WoLpH/spam') == ['release.yml is enabled and triggers on release events']

def test_main_exits_2_before_creating_anything_when_preflight_fails(monkeypatch) -> None: ...
def test_plan_versions_classifies(monkeypatch) -> None:
    # versions: tagged+released, tagged only, untagged, empty-on-pypi with tag, yanked, rc
    ... assert plans by version: tag_action, release_action, files_source, prerelease, yanked

def test_changelog_section_extracts_the_block(tmp_path) -> None:  # CHANGELOG.rst at a tag via a scratch repo or a monkeypatched `git show`
def test_notes_for_falls_back_to_stock_sentence() -> None: ...
def test_run_backfill_dry_run_creates_nothing(monkeypatch) -> None: ...
def test_run_backfill_asserts_latest_unchanged(monkeypatch) -> None:
    # latest_release returns a different tag after the run: report says so and main returns 1
def test_untagged_versions_land_on_the_untagged_release(monkeypatch) -> None: ...
def test_rebuild_failure_creates_no_release(monkeypatch) -> None: ...
def test_rebuild_runs_uv_build_in_an_archive(tmp_path, monkeypatch) -> None:  # tests/test_rebuild.py, Recorder checks argv
def test_backfill_is_registered() -> None: ...
```

- [ ] **Step 2: Run to see them fail**, **Step 3: Implement**, **Step 4: Run tests, ruff, ty**

Run: `uv run pytest -v && uv run ruff check --fix && uv run ruff format && uv run ty check`
Expected: passed, clean.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add python_project_tools tests README.md
/usr/bin/git commit -S -m 'feat: ppt backfill, every PyPI version on a signed GitHub release'
```

### Task 10: VERIFY.md, verify-release signature check, skill step, changelog

**Files:**
- Create: `VERIFY.md`
- Modify: `python_project_tools/verify_release.py` (new check
  `check_signatures(repo_or_tag...)`), `CHANGELOG.rst`, `README.md`,
  `pyproject.toml` (version `2.1.0`)
- Modify (ai repository): `~/workspace/ai/skills/release-python-package/SKILL.md`
- Test: `tests/test_verify_release.py` (signature check)

**Interfaces:**
- Produces: `verify_release.check_signatures(tag: str) -> Result`: PASS when
  the GitHub release for `tag` carries, for every dist asset (`.whl`,
  `.tar.gz`), a `.asc` and a `.sigstore.json`, plus `SHA256SUMS` and
  `SHA256SUMS.asc`. FAIL listing what is missing. SKIP when `signatures`
  is in `--skip`. The `--skip` group list becomes
  `pypi,github,stable,docs,install,conda,signatures`.

- [ ] **Step 1: Write the failing tests**

```python
def test_check_signatures_pass(monkeypatch) -> None: ...   # github.releases fake with the full asset set
def test_check_signatures_lists_missing(monkeypatch) -> None:
    res = verify_release.check_signatures('v1.0')
    assert res.status == 'FAIL' and 's.whl.sigstore.json' in res.evidence
def test_help_lists_the_skip_groups(...)  # update the existing assertion to the seven groups
```

- [ ] **Step 2: Run to see them fail**, **Step 3: Implement and write the prose**

`VERIFY.md` sections, in this order: what is on a release (the file set),
getting the key (fingerprint, `gpg --keyserver keys.openpgp.org --recv-keys
149325FD15904E9C4EB89E95E81444E9CE1F695D`, `curl https://github.com/wolph.gpg | gpg --import`),
checking a file (`gpg --verify FILE.asc FILE`, `sha256sum -c SHA256SUMS`),
checking a Sigstore bundle signed by Rick (`uvx sigstore verify identity
--bundle FILE.sigstore.json --cert-identity <identity> --cert-oidc-issuer
https://github.com/login/oauth FILE`, the identity read from the first real
bundle and filled in by the builder, with a placeholder `<identity>` until
then), checking a bundle signed by CI (plan 3, one sentence and a forward
pointer), comparing with PyPI's own attestation
(`https://pypi.org/integrity/<name>/<version>/<file>/provenance`), and
what "signed on <date>" in a release footer means. Plain register,
ASCII punctuation, British spelling, no semicolons.

SKILL.md Phase 2: after step 2 (publish workflow green) insert step 2b
"Sign: `ppt sign vX.Y.Z --gpg-only`. Downloads the PyPI files, verifies
sha256, uploads `.asc` and `SHA256SUMS.asc` to the GitHub release." and
make step 5's `ppt verify-release` call unchanged (the new check runs by
default). Also drop the stray `uv run --no-project --python '>=3.11' --with
packaging` wrapper around `ppt check-compat` near SKILL.md:100, the plain
`ppt check-compat` is enough. Commit in the ai repository, staging only
`skills/release-python-package/SKILL.md`.

`CHANGELOG.rst` gets a `2.1.0:` section listing the four commands, the
signature check and VERIFY.md. `uv version 2.1.0`.

- [ ] **Step 4: Run tests, ruff, ty, prose scan**

Run: `uv run pytest -v && uv run ruff check --fix && uv run ruff format && uv run ty check && grep -nP '[^\x00-\x7F]|;' VERIFY.md README.md CHANGELOG.rst`
Expected: passed, clean, the grep prints nothing.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add VERIFY.md CHANGELOG.rst README.md pyproject.toml python_project_tools tests
/usr/bin/git commit -S -m 'feat: VERIFY.md, signature check in verify-release, 2.1.0'
```

### Task 11: Canary on tissue, then the rest (Rick present)

**Files:** none in the repository, except the report.
- Create: `docs/superpowers/plans/2026-11-06-backfill-report.md`

This task runs the tool. It needs Rick at the keyboard: the Sigstore step
opens a browser for the GitHub login per package, and the GPG agent must be
unlocked. Before it starts, the prerequisites from the spec are done: the
GPG public key on GitHub is current, and python-progressbar's `release.yml`
is disabled with `gh workflow disable release.yml -R WoLpH/python-progressbar`
before that package's run (and left disabled, plan 3 replaces it).

- [ ] **Step 1: Inventory everything once**

Run: `ppt inventory --json <22 packages> > docs/superpowers/plans/2026-11-06-inventory-before.json`
Expected: 22 entries, totals match the spec (566 versions with files, 153
untagged). Commit the file.

- [ ] **Step 2: Dry run on the canary**

Run: `ppt backfill tissue --checkout ~/workspace/tissue --dry-run`
Expected: a plan with 11 versions, 7 with tags, 4 untagged, nothing
created. Read the plan with Rick.

- [ ] **Step 3: Real run on the canary**

Run: `ppt backfill tissue --checkout ~/workspace/tissue`
Expected: exit 0, `latest_after == latest_before`, every version row
`signed`. Then `ppt inventory tissue` shows `asc`, `sigstore` and `sums`
on every released version, and `gpg --verify` plus `uvx sigstore verify
identity` succeed on one downloaded asset by hand. Rick looks at one
release page before Step 4.

- [ ] **Step 4: The remaining 21 packages, smallest first**

Order: zfs-utils-osx, spotimote, slack_notifier, Sphinx-PyPI-upload-2,
cache_toolbar, formatter, formatter2, mailjet, django-redis-admin,
django-admin-generator, speedups, python-statsd, django-statsd, tissue is
done, django-utils2, django-tags-input, mt-940, python-utils, numpy-stl,
portalocker, progressbar2 (release.yml disabled first). One `ppt backfill`
per package, each report appended to
`docs/superpowers/plans/2026-11-06-backfill-report.md`, each inventory
re-run afterwards. A failed version is noted and the run continues.

- [ ] **Step 5: Commit the report and the after-inventory**

```bash
ppt inventory --json <22 packages> > docs/superpowers/plans/2026-11-06-inventory-after.json
/usr/bin/git add docs/superpowers/plans
/usr/bin/git commit -S -m 'docs: backfill report and inventories before and after'
```
