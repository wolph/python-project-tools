"""Sigstore bundles for ``ppt sign`` from the repository's sign.yml.

A dist counts as signed when its bundle on the release verifies against the
CI identity or Rick's. Missing bundles come from one dispatch of sign.yml,
which uploads the missing dists and their bundles itself, after which the
release is read again. Call the other modules through their attributes
(``ci_sign.dispatch``, ``identities.verify_bundles``) so tests can replace
them.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import pathlib

from python_project_tools import ci_sign, common, github, identities, signing

BUNDLES_DIR: str = 'bundles'
PREDATES: str = 'predates sign.yml, sign with --sigstore-browser'


class NoWorkflowError(RuntimeError):
    """The default branch of the repository has no sign.yml."""


@dataclasses.dataclass(frozen=True)
class Workflow:
    """The branch sign.yml runs from and the day it arrived there."""

    branch: str
    since: dt.date


def check_workflow(repo: str) -> Workflow:
    """The sign.yml of ``repo``, raising NoWorkflowError when it has none."""
    branch: str = ci_sign.default_branch(repo)
    since: dt.date | None = ci_sign.workflow_since(repo, branch)
    if since is None:
        raise NoWorkflowError(
            f'no sign.yml on {branch} of {repo}: copy templates/sign.yml '
            'from python-project-tools, or pass --sigstore-browser'
        )
    return Workflow(branch=branch, since=since)


def bundle_name(dist_name: str) -> str:
    """The asset name of the Sigstore bundle of a dist."""
    return f'{dist_name}{signing.BUNDLE_SUFFIX}'


def created_on(release: github.Release) -> dt.date | None:
    """The day a release was created, None when GitHub gave no date."""
    if not release.created_at:
        return None
    try:
        stamp: dt.datetime = dt.datetime.fromisoformat(
            release.created_at.replace('Z', '+00:00')
        )
    except ValueError as error:
        raise signing.SigningError(
            f'release {release.tag} has an unreadable creation date: '
            f'{release.created_at!r}'
        ) from error
    return stamp.date()


def refusal(
    release: github.Release, rebuilt: bool, workflow: Workflow
) -> str | None:
    """Why sign.yml may not sign this release, None when it may.

    sign.yml signs only the PyPI files of a release created on or after the
    day it arrived. A release without a creation date counts as older.
    """
    created: dt.date | None = created_on(release)
    if rebuilt or created is None or created < workflow.since:
        return PREDATES
    return None


def _missing_bundles(
    release: github.Release,
    dists: list[pathlib.Path],
    workdir: pathlib.Path,
    repo: str,
) -> list[str]:
    """Verify the bundles the release has, return the dists without one.

    Raises SigningError naming every bundle that matches no accepted
    identity, so a bad bundle is never answered with a dispatch whose
    upload would collide with it.
    """
    assets: dict[str, github.Asset] = {a.name: a for a in release.assets}
    checks: list[identities.BundleCheck] = []
    missing: list[str] = []
    for path in dists:
        asset: github.Asset | None = assets.get(bundle_name(path.name))
        if asset is None:
            missing.append(path.name)
            continue
        bundle: pathlib.Path = github.download_asset(
            asset, workdir / BUNDLES_DIR
        )
        checks.append(
            identities.BundleCheck(path.name, common.sha256_of(path), bundle)
        )
    if checks:
        verdicts: dict[str, identities.Identity | None] = (
            identities.verify_bundles(checks, repo)
        )
        invalid: list[str] = [
            c.name for c in checks if verdicts[c.name] is None
        ]
        if invalid:
            raise signing.SigningError(
                'invalid bundle on the release: ' + ', '.join(invalid)
            )
    return missing


def _reread(repo: str, tag: str) -> github.Release:
    for item in github.releases(repo):
        if item.tag == tag:
            return item
    raise signing.SigningError(
        f'release {tag} is gone from {repo} after the {ci_sign.WORKFLOW} run'
    )


@dataclasses.dataclass(frozen=True)
class Target:
    """One release for sign.yml: what it holds and where it comes from."""

    release: github.Release
    dists: list[pathlib.Path]
    rebuilt: bool
    conflicts: list[str]


def ensure(
    target: Target,
    workdir: pathlib.Path,
    repo: str,
    package: str,
    workflow: Workflow,
) -> github.Release | None:
    """Give every dist of the release an accepted bundle.

    Returns the release read again after a sign.yml run, or None when every
    bundle was there already and nothing was dispatched. Raises SigningError
    when a bundle is invalid, when sign.yml may not sign the release, when
    its run fails and when the run leaves a bundle out.
    """
    release: github.Release = target.release
    missing: list[str] = _missing_bundles(release, target.dists, workdir, repo)
    if not missing:
        return None
    reason: str | None = refusal(release, target.rebuilt, workflow)
    if reason is not None:
        raise signing.SigningError(reason)
    if target.conflicts:
        # sign.yml fails on a release asset that differs from PyPI.
        raise signing.SigningError(
            f'conflicting assets stop {ci_sign.WORKFLOW}: '
            + ', '.join(target.conflicts)
        )
    run: ci_sign.Run = ci_sign.dispatch(
        repo, release.tag, package, workflow.branch
    )
    ci_sign.watch(repo, run)
    fresh: github.Release = _reread(repo, release.tag)
    waited: list[pathlib.Path] = [p for p in target.dists if p.name in missing]
    still: list[str] = _missing_bundles(fresh, waited, workdir, repo)
    if still:
        raise signing.SigningError(
            f'{ci_sign.WORKFLOW} ran but left no bundle for '
            + ', '.join(still)
        )
    return fresh
