# CI Sigstore signing rollout ledger

sign.yml PRs opened 2026-11-16, each adds or updates only .github/workflows/sign.yml (identical to templates/sign.yml at df9a555), merged only on Rick's request.

- django-admin-generator https://github.com/wolph/django-admin-generator/pull/30
- django-redis-admin https://github.com/wolph/django-redis-admin/pull/14
- django-statsd https://github.com/wolph/django-statsd/pull/29
- django-tags-input https://github.com/wolph/django-tags-input/pull/39
- django-utils https://github.com/wolph/django-utils/pull/15
- mt940 https://github.com/wolph/mt940/pull/145
- numpy-stl https://github.com/wolph/numpy-stl/pull/256
- portalocker https://github.com/wolph/portalocker/pull/161
- python-statsd https://github.com/wolph/python-statsd/pull/61
- python-progressbar https://github.com/wolph/python-progressbar/pull/341
- speedups https://github.com/wolph/speedups/pull/22
- python-utils https://github.com/wolph/python-utils/pull/80

## Merged 2026-11-16 (squash, pinned to the tested head, sign.yml identical to templates/sign.yml at e706651)

- django-admin-generator #30 edb3791, django-statsd #29 4a5b010, django-tags-input #39 b2ef027, django-utils #15 57f62f3, numpy-stl #256 d13b136, portalocker #161 03ec040, python-statsd #61 f47baab, python-progressbar #341 12f734a, speedups #22 9c955e5, python-utils #80 708ef32.
- mt940 #145 9922d01 and django-redis-admin #14 67fa800, after the soupsieve fix for CVE-2026-126190 (mt940 #146 e8b6488, django-redis-admin #15 f41ec97) let soupsieve 4.1.1 through the 14-day uv cooldown. Drop that exclude-newer-package entry after 2026-11-28.

All 12 repositories carry sign.yml identical to templates/sign.yml at e706651.
