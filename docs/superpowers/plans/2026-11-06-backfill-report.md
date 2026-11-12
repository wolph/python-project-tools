# Backfill report: signed GitHub releases for every PyPI version

Plan 2 ran `ppt backfill` over every PyPI package with a repository under WoLpH, from 2026-11-06 to 2026-11-12. Every version with files on PyPI now has a GitHub release that carries those exact files, a `.asc` GPG signature and a Sigstore bundle per file, a GPG-signed `SHA256SUMS`, and a footer that says when and how it was signed. `VERIFY.md` shows how to check them.

## Result per repository

The audit behind this table reads the asset names of every release: a dist file counts as complete when its `.asc` has a `.sigstore.json` next to it, and a release counts as complete when all its dist files are and its notes carry the footer.

| Repository | PyPI package | Releases | Complete |
| --- | --- | --- | --- |
| tissue | tissue | 11 | 11 |
| sphinx-pypi-upload | Sphinx-PyPI-upload-2 | 5 | 5 |
| supervisord-slack-notifier | slack_notifier | 1 | 1 |
| Cache-Debug-Toolbar | cache_toolbar | 2 | 2 |
| spotimote | spotimote | 1 | 1 |
| zfs-utils-osx | zfs-utils-osx | 1 | 1 |
| django-redis-admin | django-redis-admin | 4 | 4 |
| python-formatter | formatter2 | 6 | 6 |
| speedups | speedups | 15 | 15 |
| django-admin-generator | django-admin-generator | 14 | 14 |
| python-statsd | python-statsd | 29 | 29 |
| django-statsd | django-statsd | 25 | 25 |
| django-utils | django-utils2 | 36 | 36 |
| django-tags-input | django-tags-input | 28 | 28 |
| mt940 | mt-940 | 44 | 44 |
| portalocker | portalocker | 52 | 52 |
| python-utils | python-utils | 64 | 64 |
| numpy-stl | numpy-stl | 61 | 61 |
| python-progressbar | progressbar2 | 109 | 109 |
| mailjet | mailjet | 7 | GPG only |

Latest points at the highest stable version on every repository, checked before and after each run.

## What was left out, and why

- `formatter` on PyPI: the predecessor of formatter2, whose repository no longer exists, and its tag v1.0.3 would collide with formatter2's. Skipped by decision.
- mailjet: archived again during the run, not by the tooling. Its releases carry the PyPI files, `.asc` signatures and a signed `SHA256SUMS`, but no Sigstore bundles, because uploading needs a writable repository. Left as it is by decision.
- portalocker 0.1 and progressbar2 0.1: PyPI lists the version but has no files for it, and there is no tag to rebuild from.
- numpy-stl 4.1.2: PyPI has no files left, and the tag cannot be rebuilt because the tree holds a symlink to an absolute path, which the safe tar extraction refuses.
- progressbar2 2.3 and 3.0: PyPI has no files left, and their `setup.py` no longer runs on a current Python (`__slots__` conflicts with a class variable, and `collections.Iterable` is gone).
- speedups 1.1.0 (`speedups-1.1.0-py3.11-macosx-12-x86_64.egg`) and portalocker 2.7.0 (`portalocker-2.7.0-py3-none-any.whl`): files uploaded to the release by hand in 2023 with their own `.asc` of that day. They are not on PyPI, so the backfill leaves them as they are.

## Files that were replaced

Twenty progressbar2 releases, v3.34.2 to v4.6.0, carried files uploaded to GitHub between 2017 and 2019 whose bytes differ from what PyPI serves under the same names. By decision they were replaced with the PyPI bytes and signed. The old copies, 74 files with a sha256 list, were downloaded first. Earlier runs replaced conflicting files on other repositories the same way.

## Changes outside the releases

- Tags were created for versions without one, always on a commit GitHub already has. No existing tag was moved or deleted.
- Six archived repositories were unarchived and left open, by decision.
- python-statsd's `publish.yml` was disabled during the runs so that creating releases could not publish anything, and enabled again afterwards. python-progressbar's `release.yml` stays disabled.
- Nothing on PyPI was changed, and no workflow ran.

## Open points

- Several checkouts have local tags that differ from the GitHub tags of the same name: mt940, python-utils, django-statsd, django-utils, django-tags-input, django-admin-generator and portalocker. The tooling reads GitHub's tags from a private ref namespace and left the local ones alone. They are worth a look by hand.
- Parked defects, listed with their rulings in the ledger: `--replace-assets` re-signs releases that are already complete, a resumed release downloads its files twice, and a release completed by a footer edit alone reports "already signed".
- Plan 3, signing future releases in GitHub Actions with Sigstore while GPG stays local, is not written yet.
