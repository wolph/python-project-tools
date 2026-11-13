# Signed releases for every PyPI version, past and future

Date: 2026-11-06. Author: Rick van Hattem, written up with Claude from a
brainstorming session. Status: builder check done, awaiting approval.

Revised 2026-11-13: future releases get their Sigstore bundles from a
`sign.yml` workflow that `ppt sign` dispatches, so no release needs a
browser login any more. Section 4 is rewritten, and decision 5, the `ppt
sign` command, section 5, the prerequisites and the risks follow it.

## Intent

Anyone who installs one of Rick's packages should be able to check that the
wheel or sdist they got is the one Rick published. PyPI stopped accepting GPG
signatures in 2023, so today there is nothing to check against for most
versions: the recent ones carry a PEP 740 attestation from Trusted Publishing,
the older ones carry nothing at all, and nothing anywhere carries Rick's own
key.

The fix has two halves. A backfill signs every version that already exists and
attaches the signed files to a GitHub release on the matching tag. A change to
the release pipeline makes every future release come out signed the same way
without a separate step anyone has to remember.

The tooling for both halves lives in `python-project-tools`, which also
becomes the home for the scripts that today sit inside the
`release-python-package` skill.

## Goals

1. Every PyPI version of every package in scope has a GitHub release on its
   tag, carrying the exact PyPI files, a detached GPG signature per file, a
   Sigstore bundle per file, and a signed `SHA256SUMS`.
2. Every future release gets the same set automatically: Sigstore and the dist
   files from CI, GPG from the local release step.
3. One command re-run later converges: it adds what is missing and touches
   nothing that is already correct.
4. A reader can verify a release with the instructions linked from its notes,
   using only public key servers and the Sigstore public log.
5. `python-project-tools` is a modern, tested package that carries
   this tooling and the release scripts the skills use today.

## Non-goals

- Changing anything on PyPI. No re-uploads, no yanks, no metadata edits.
- Moving or deleting any existing git tag. Existing tags are history.
- Signing packages Rick merely co-owns on PyPI (the jorenham packages,
  bson_lazy, fastdupes, onkyo-eiscp, django-denorm, newrelic-python-agent),
  or the four packages without a repository (dinner, dropbox2, numpy_turtle,
  tcc). They stay as they are.
- Migrating the merge-prs and hunt-bugs scripts. They are PR review tooling,
  not release tooling. Later candidate, not this round.
- Publishing python-project-tools to PyPI. Rick's words: it is his own tool
  for now, unless it clearly proves to be a good reusable tool for others. It
  is installed from the checkout or the git URL, and nothing in CI depends on
  it.

## Anti-goals

Things that would make this a failure even if every file gets signed:

- A backfilled release of an old version showing up as "Latest" on GitHub.
- A publish workflow firing because a tag was pushed or a release was created
  during the backfill, and re-publishing or re-releasing anything.
- An existing release asset with different bytes being replaced silently.
- A signature or release note that reads as if it was made on the original
  release date. Everything backfilled says so, with the signing date.
- Rick's GPG private key, or any subkey, ending up in GitHub secrets.
- A version matched to the wrong commit and tagged as if it were that commit.

## What exists today (recon, 2026-11-06)

- PyPI: 37 packages with Rick as Owner, 648 versions, 1395 files. In scope: 22
  packages on WoLpH repositories, 566 versions with files.
- Tags: 153 of those 566 versions have no tag. Worst: django-statsd 19,
  progressbar2 18, django-tags-input 17, numpy-stl 15. Six repositories have
  no tags at all (python-formatter, zfs-utils-osx, spotimote,
  Cache-Debug-Toolbar, supervisord-slack-notifier, sphinx-pypi-upload).
- Tag styles differ: `vN` dominates, older tags use bare `N`, portalocker has
  `untagged-N`, python-statsd has a stray `ci` tag, progressbar has one `vvN`.
- Releases: about 250 exist. Recent ones (portalocker v4.4.2, progressbar2
  v4.7.0, python-utils v4.2.0) have no assets. Older ones have assets uploaded
  by hand, and speedups has full cibuildwheel wheel sets (308 assets).
- Signing today: recent PyPI uploads carry PEP 740 Sigstore attestations from
  Trusted Publishing (verified on progressbar2 4.7.0 via the integrity API).
  No GPG anywhere. Tags are GPG signed with key
  `149325FD15904E9C4EB89E95E81444E9CE1F695D`.
- That key is valid locally until 2031-02-01 and is on keys.openpgp.org and
  keyserver.ubuntu.com, but the copy GitHub holds expired on 2023-07-31. Every
  signed tag shows as "expired key" on GitHub until the public key is
  re-uploaded.
- Workflows: eleven repositories share a `publish.yml` triggered by a `v*` tag
  push (preflight, tests, build, Trusted Publishing, `gh release create`,
  master fast-forward). django-redis-admin's variant also runs
  `gh release upload dist/*`. python-progressbar instead has `release.yml`
  on `release: published` that builds and publishes to PyPI without
  skip-existing. speedups builds wheels on every push and uploads them to the
  release in `build_wheels.yml`.
- Tooling present: gpg 2.x, `sigstore` 4.5.1 via `uvx`, no cosign.
- python-project-tools: archived on GitHub, last commit 2012, setup.py, nose,
  Travis. The name is free on PyPI. Its 2012 README already promised GPG
  signed releases, which is a tidy coincidence.
- The release-python-package skill (in `~/workspace/ai/skills/`) bundles
  `verify_release.py`, `check_compat.py` with `api_manifest.py` and
  `compat_rules.py`, and `conda_forge.py`, about 2600 lines plus 600 lines of
  tests. `scripts/check_distribution.py` is copied into the package
  repositories and run by publish.yml.

## Decisions Rick made

1. Scope: the 22 packages on WoLpH repositories only.
2. Source of the signed bytes: the exact PyPI files wherever PyPI has them,
   verified by sha256 against the PyPI JSON before signing. A rebuild from
   the tag only where PyPI has no files for that version.
3. Signing: both GPG with the existing key and Sigstore with Rick's GitHub
   identity. The Sigstore transparency log fixes the signing date
   independently, which keeps the retroactive nature visible.
4. Versions without a tag: find the commit that produced the version and
   tag it. A version whose commit cannot be matched gets no tag, its signed
   files go into one `untagged` release per repository, and the report lists
   it.
5. Future releases: Sigstore in CI, GPG as the last step of the
   release-python-package skill on Rick's machine. The key stays local. A
   release that only ran through CI picks up its GPG signatures the next time
   the sign command runs. Revised 2026-11-13: Rick wants every release fully
   signed and no browser login in the workflow at all, so the local step
   never signs with Sigstore itself. When bundles are missing it has CI make
   them (section 4).
6. Home of the tooling: python-project-tools, fully modernised. Dearchive,
   drop the 2012 code (it stays in history), new package with pyproject, uv,
   ruff, ty, pytest, the same publish.yml as the other repositories, and a
   CLI.
7. The reusable release skill scripts move into the package.
8. Existing releases get the verification footer appended to their notes as
   well. The original text stays, the footer goes under it.
9. python-project-tools stays off PyPI. It is Rick's own tool until it proves
   itself reusable for others.
10. Rollout of the future-release pipeline: the eleven repositories with
    publish.yml, python-progressbar, and speedups (where the signing step goes
    into build_wheels.yml so the cibuildwheel wheels are signed too). The six
    dormant repositories without tags get GPG through the skill if a release
    ever happens there, and nothing else.

## Calls made in the write-up (Claude's, check these)

- CLI name `ppt`, module `python_project_tools`, distribution name
  `python-project-tools`. One console script with subcommands.
- GPG output is a detached ASCII-armoured `<file>.asc` per file, plus
  `SHA256SUMS` and `SHA256SUMS.asc` per release. Sigstore output is
  `<file>.sigstore.json` per file. Nothing else gets invented.
- Rebuilt files keep the standard filename that `uv build` produces. The
  release notes name each rebuilt file and `SHA256SUMS` carries a comment line
  `# rebuilt from tag <tag> on <date>, not the PyPI bytes` above them. A
  rebuild that fails on a modern interpreter is reported and no release is
  created for it, so the report shows the gap and GitHub does not fill up
  with empty releases.
- The version set per repository is the union of PyPI versions with files
  and version-shaped tags (`vN`, `N`) without a PyPI version. The second
  group is Rick's "rebuild where it is not on PyPI". Tags that are not
  version-shaped (`ci`, `vvN`, `untagged-N`) are listed by `inventory` and
  otherwise ignored.
- The footer on an existing release is appended under the original notes
  with an HTML comment marker, so a second run finds it and does not append
  twice. The original text is never edited.
- CI does not depend on ppt. The release job in publish.yml downloads the
  files for the tag from PyPI after publishing, checks their sha256 against
  the PyPI JSON, signs them with `sigstore/gh-action-sigstore-python`, and
  uploads files, bundles and `SHA256SUMS` to the release. Signing the PyPI
  bytes rather than `dist/` matters: with skip-existing a re-pushed tag can
  leave PyPI holding older bytes than a fresh `dist/`. The shell for this
  lives in the publish.yml template in python-project-tools, and
  `scripts/check_distribution.py` stays a copied script in each repository
  with the template as its single source.
- Proof of the future-release half: the next real release of one small
  package (python-statsd or tissue, whichever releases first) runs the new
  publish.yml as the live test, with its result checked by `ppt
  verify-release`. The other rollouts follow a green result. CI has
  no dependency on ppt, so nothing needs to be installable from a URL first.
- ppt is installed locally with `uv tool install --editable
  ~/workspace/python-project-tools`, so the skills can call `ppt` directly
  and a checkout change is live without reinstalling. The skill text says so
  once, in the setup line.
- Yanked versions are included and marked yanked in the notes. Pre-release
  versions (per PEP 440) are created with the GitHub pre-release flag.
- Release notes body: the changelog section for that version when the
  repository's changelog has one at the tag, found with the same rule
  publish.yml uses, otherwise one stock sentence. Every backfilled release
  ends with a fixed footer: signed retroactively on <date>, files downloaded
  from PyPI and verified by sha256, GPG fingerprint, link to VERIFY.md.
- Existing assets: an asset with the same name and the same sha256 as the
  PyPI file is kept. One with the same name and different bytes is reported
  and left alone unless `--replace-assets` is passed. Assets with other names
  (hand-uploaded wheels, the speedups cibuildwheel sets) are never touched.
- New tags follow the repository's current style (`vN` everywhere in scope),
  are annotated and GPG signed, and the message states the PyPI version, the
  sha256 of the sdist it was matched against, and the matching date. The
  tagger date is the real date. No backdating.
- The `untagged` release per repository hangs on a signed annotated tag
  `pypi-untagged-releases` on the repository's root commit, because a GitHub
  release needs a tag and the root commit is the one place that is not
  claiming to be any particular version. portalocker's existing
  `untagged-N` tags are left alone and not reused.
- Commit matching for an untagged version: candidates come from `git log -S`
  on the version string over the files that ever held the version (setup.py,
  `__init__.py`, `__about__.py`, pyproject.toml, setup.cfg). A candidate
  matches when every file in the sdist that is tracked in that tree is
  byte-identical, ignoring generated files (PKG-INFO, egg-info, setup.cfg
  written by tooling). Several matching commits: take the one closest in time
  to the PyPI upload. No match: untagged.
- Pushing a new tag onto a commit whose tree contains a workflow with a tag
  trigger is refused by default (the publish workflow would run on the old
  commit). `--allow-workflow-trigger` overrides for a case Rick looks at.
- Creating a release in a repository whose default branch holds a workflow
  with a `release:` trigger is refused until that workflow is disabled
  (`gh workflow disable`). This is what stops python-progressbar's
  release.yml from firing 112 times. The tool checks, it does not disable
  anything itself.
- Every backfilled release is created with `--latest=false`, and after a
  package's backfill the tool asserts that the "Latest" release is still the
  highest stable version.
- The release skill text in the `ai` repository changes from
  `python -I <skill-dir>/scripts/x.py` to `ppt <command>`. The scripts and
  their tests are removed from the skill directory in the same change, so
  there is one copy.
- Idempotency doubles as catch-up: `ppt release sign --all` over a repository
  signs any release still missing `.asc` files, which covers a release that
  only ran through CI. Rick chose "local via the skill" over a separate
  catch-up script, and this gives the same effect for free, so I am noting it
  rather than building a second mechanism.

## The shape of the work

### 1. python-project-tools revival

Dearchive the repository. Remove the 2012 tree. New layout matching Rick's
other packages (the modernize-python skill's reference files describe the
target: pyproject with uv_build, ruff, ty, pytest, lefthook, autofix.ci,
CodeQL, stale, the shared publish.yml, CHANGELOG, Sphinx docs optional). Python
floor: the same as portalocker's current floor. Dependencies kept small:
`packaging`, `httpx` or stdlib urllib, `click` or argparse. The builder picks,
with argparse as the default if nothing argues otherwise, because the skill
scripts already use it.

The package does not publish anywhere. Its own CI runs the tests, ruff and
ty, nothing more.

### 2. CLI surface

Commands the design needs. Exact grouping and flag names are the builder's.

- `ppt inventory [PACKAGE...]`: the recon tables. PyPI versions, files,
  yanked, tags matched, releases present, assets present, signatures present.
  Markdown and JSON output. This is also the progress report of the backfill.
- `ppt match-tag PACKAGE VERSION`: find the commit for an untagged version,
  print the evidence, optionally create and push the signed tag.
- `ppt sign TAG | --all`: for a release, download the PyPI files for that
  version into a fresh empty directory, verify sha256 against PyPI, GPG sign,
  Sigstore sign, write SHA256SUMS, upload what is missing. Used by the
  backfill and by the release skill. `--gpg-only` and `--sigstore-only` so
  the unattended GPG half and the interactive Sigstore half can run apart,
  and so the skill adds only GPG to a release CI already Sigstore-signed.
  Revised 2026-11-13: for future releases the Sigstore half comes from CI,
  as section 4 describes, and the browser signer is opt-in only.
- `ppt backfill PACKAGE [--only VERSION] [--dry-run]`: the loop over every
  PyPI version: tag lookup, match, release creation, sign. Prints the report.
  Dry run prints every action it would take and creates nothing.
- `ppt verify-release`, `ppt check-compat`, `ppt conda-forge`,
  `ppt check-distribution`: the moved skill scripts, behaviour unchanged,
  tests moved with them. `verify-release` gains a check that the GitHub
  release carries the dist files, `.asc`, `.sigstore.json` and `SHA256SUMS`.

### 3. Backfill procedure per package

1. Resolve the repository (the PyPI project URLs, with an override table for
   the three packages whose URLs point at sponsors/wolph).
2. Refuse if the default branch has a `release:`-triggered workflow that is
   enabled.
3. For each PyPI version with files, oldest first: existing tag by normalised
   name, else `match-tag`, else untagged.
4. Create the release when missing: title is the tag, body as described,
   `--verify-tag --latest=false`, pre-release flag from PEP 440.
5. `ppt sign` for the release.
6. For a tag with no PyPI version, and a PyPI version with an empty file
   list: rebuild in an isolated checkout of the tag with `uv build`, mark as
   rebuilt, sign, upload. Failure is reported, not retried.
7. Untagged versions: collect, create the `pypi-untagged-releases` tag and
   release once, upload their signed files, list them in the body.
8. Assert "Latest" is unchanged. Print the report.

Order of packages: start with a small one (tissue, 11 versions, 4 untagged)
as the canary, then the rest. The canary run is where Rick looks at the
result before the remaining 555 versions go.

### 4. Future releases

Revised 2026-11-13. The first version of this section put the signing steps
into the shared publish.yml. That workflow turned out not to be shared in
practice: python-utils publishes on a `v*` tag push, python-statsd on
`release: published`, python-progressbar through release.yml, and in most
repositories the GitHub release is created by the skill after the publish run,
so a step inside publish.yml would find no release to upload to. Signing moves
into a workflow of its own, which does not care how the package reached PyPI.
publish.yml, release.yml and build_wheels.yml stay as they are.

#### sign.yml

One file, identical in every repository, with a template in
python-project-tools (`templates/sign.yml`) as its single source.

- Trigger: `workflow_dispatch` only, with two required inputs, `tag` and
  `package` (the PyPI name). It always runs from the default branch.
- Permissions: `contents: write` for release assets and `id-token: write` for
  Sigstore. No environment, no secrets, no other permission.
- Steps:
  1. Fail unless a GitHub release exists for `tag`.
  2. Download every file PyPI lists for the version into an empty directory
     and check each sha256 against the PyPI JSON. Fail when PyPI lists no
     files or a hash differs.
  3. Skip a file whose `<file>.sigstore.json` on the release already verifies
     against that file with one of the two accepted identities (below).
  4. Sign the rest with `sigstore/gh-action-sigstore-python`, pinned by
     commit sha, writing `<file>.sigstore.json`.
  5. Upload the dist files the release lacks and the new bundles, never
     with `--clobber`. An asset with the same name and different bytes fails
     the run and names the asset.
- sign.yml does not write `SHA256SUMS`. `ppt sign` owns `SHA256SUMS` and
  `SHA256SUMS.asc` as a pair, as it does today, and replaces the pair when
  the set of files changed (a late wheel, a partial first run). A late file
  only needs a new sign.yml run for its own bundle.
- Certificate identity: `https://github.com/WoLpH/<repo>/.github/workflows/sign.yml@refs/heads/<branch>`,
  issuer `https://token.actions.githubusercontent.com`, trigger
  `workflow_dispatch`. Any branch of that repository is accepted, because
  every default branch is `develop` today and may be renamed to `master`
  later, and an exact pin would then fail every earlier bundle. Push access
  to any branch is the same trust as push access to the default one. ppt
  dispatches on the default branch. This identity differs from the one on
  PyPI's own attestation, which names publish.yml.

What a CI bundle proves: this repository's sign.yml saw these exact bytes on
PyPI and the transparency log recorded it on that date. It does not prove
Rick ran it. The GPG signature, which only Rick's machine can make, carries
that.

#### ppt sign, local

`ppt sign vX.Y.Z` is the release skill's step 3b and needs no browser:

1. Download the PyPI files into a fresh empty directory and verify sha256, as
   today. PyPI must already list the files. The command does not wait for
   them.
2. Check every dist file for a bundle on the release that verifies with one
   of the accepted identities.
3. When any are missing or fail, check that sign.yml exists on the default
   branch (stop otherwise, naming the template and the repository), run
   `gh workflow run sign.yml -f tag=... -f package=...`, find that run, follow
   it with `gh run watch --exit-status` and check the bundles again. A failed
   run stops ppt with the run URL. No automatic retry.
4. Write `SHA256SUMS` over the verified PyPI files, sign each dist file and
   `SHA256SUMS` with GPG, and upload the `.asc` files with `SHA256SUMS`, as
   today.
5. Append the verification footer, as today.

`--gpg-only` keeps its meaning and skips steps 2 and 3. `ppt sign --all`
dispatches sign.yml only for releases created on or after the day sign.yml
landed in that repository. An older release that lacks a bundle, and any
rebuilt file (PyPI lists none, so sign.yml cannot sign it), goes into the
report instead, so retroactive signing stays under Rick's identity and
visibly retroactive. The browser signer
stays in the package behind an explicit `--sigstore-browser` flag, for a
repository without Actions or an Actions outage. The skill passes it in one
case only, by Rick's decision on 2026-11-13: a release in a repository whose
sign.yml PR is not merged yet, so every release is fully signed during the
rollout and the browser disappears per repository once its PR lands.

The skill text for step 3b becomes `ppt sign vX.Y.Z` with "no browser" and
the "becomes --gpg-only" sentence goes.

#### Accepted identities

Per dist file, `ppt verify-release` and `ppt sign` accept a bundle that
verifies against either:

- the CI identity of that repository's sign.yml, as above, or
- Rick's identity (`wolph@wol.ph`, issuer `https://github.com/login/oauth`),
  which the backfilled bundles carry.

Any other identity fails. `verify-release` keeps failing a release that lacks
a bundle, an `.asc` per dist file, `SHA256SUMS` or `SHA256SUMS.asc`.

#### Rollout

1. Template and ppt changes land in python-project-tools.
2. Pilot: python-utils, by Rick's choice on 2026-11-13. sign.yml lands there
   first, then WoLpH/python-utils#78 is merged and released, and that
   release runs the whole chain, checked by `ppt verify-release`. tissue has
   no workflows and is dropped from the pilot.
3. After a green pilot, one PR per repository adds sign.yml to the other
   repositories of decision 10, each watched to green and merged only when
   Rick asks. For speedups, sign.yml signs the PyPI files, cibuildwheel wheels
   included, and leaves the hand-uploaded wheel sets alone.
4. The python-progressbar migration from release.yml to publish.yml is no
   longer needed for signing and leaves this plan.

### 5. VERIFY.md

One page in python-project-tools, linked from every release footer. How to
fetch the key (fingerprint, both key servers, `https://github.com/wolph.gpg`
once the key there is current), `gpg --verify`, `sha256sum -c SHA256SUMS`,
`sigstore verify identity` with Rick's identity and issuer for backfilled
files, `sigstore verify github` with the sign.yml identity from section 4 for
CI-signed files (filling the "Checking a bundle signed by CI" placeholder),
and how to compare with PyPI's own attestation, which names publish.yml
rather than sign.yml. Plain register,
ASCII punctuation, British spelling, as everything else Rick writes.

## Prerequisites that only Rick can do

1. Re-upload the current public key to GitHub (Settings, SSH and GPG keys),
   or grant `gh auth refresh -h github.com -s admin:gpg_key` so the tool can.
   Until then every signed tag shows as expired there.
2. `gh repo unarchive WoLpH/python-project-tools`, or approve the tool doing
   it.
3. Be present for the Sigstore sign step of the backfill: it opens a browser
   for the GitHub OIDC login and the resulting certificate is valid for about
   ten minutes, which covers one package's files per login. Twenty-two logins
   in total, or fewer if several small packages share one. Revised
   2026-11-13: this was for the backfill only, which is done. Future releases
   need no login.
5. Merge the sign.yml PR in each repository (merged only on Rick's request).
4. Disable python-progressbar's release.yml before its backfill (`gh workflow
   disable release.yml`), or approve the tool asking for it.

## Left to the builder

- Argument parser, output formatting, logging, exit codes.
- The exact sha-compare strategy for existing assets (download and hash, or
  trust size and name first and hash on a size match).
- How `sign --all` finds releases missing signatures without downloading
  every asset twice.
- The isolated checkout and interpreter choice for rebuilds.
- Test fixtures: a fake PyPI JSON, a scratch git repository with tags, and a
  recorded `gh` interaction layer, so `backfill --dry-run` is unit-testable
  without the network.
- The rollout mechanics of sign.yml to the repositories of decision 10 (one
  PR per repository, each watched to green, as the git rules in CLAUDE.md
  require).
- How ppt finds the run it dispatched (`gh workflow run` prints no run id),
  and how sign.yml checks an existing bundle (the sigstore CLI in the job or
  the action's own verify step).
- Whether `inventory` caches PyPI JSON on disk between runs.

## Risks worth naming

- The git tag `-S` matching can produce a false match on a repository where
  the version string was bumped and the release cut from a different branch.
  The byte comparison against the sdist is the guard, and "closest to the
  upload date" only breaks ties between commits that all pass it.
- Sigstore's OIDC flow is interactive. The backfill cannot run unattended for
  the Sigstore half. The GPG half can, with the agent unlocked. For future
  releases sign.yml removes that: the runner gets its OIDC token without a
  browser.
- The public Sigstore instance answering HTTP 429 or being down fails the
  sign.yml run. ppt stops with the run URL, and running `ppt sign` again later
  dispatches a run that skips the bundles already made. An Actions outage
  leaves the release unsigned until then, with `--sigstore-browser` as the
  last resort.
- Anyone who can push to the default branch can change sign.yml and sign
  whatever they like under the CI identity. That identity is worth exactly
  as much as push access to the repository, which is why the GPG signature
  stays required next to it.
- GitHub rate limits: around 566 release creations and roughly 4000 asset
  uploads. Well under the hourly limits for an authenticated user when spread
  over a session, and the tool resumes where it stopped because it is
  idempotent.
- speedups has 308 hand-uploaded wheel assets across 13 releases that do not
  come from PyPI in the same form. The tool leaves them alone and signs the
  PyPI files next to them. The report will show the two sets side by side so
  Rick can decide later whether the cibuildwheel sets need signing too.
