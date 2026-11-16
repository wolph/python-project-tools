2.2.2:

 * ``ppt check-compat`` works again for a package that imports a dependency
   at import time, such as numpy-stl with numpy. Since 2.0.0 the API
   manifest ran with ``-S``, which hides the site-packages of the venv, so
   the import failed and the check stopped with exit status 2. The manifest
   still runs with ``-S``, and the site-packages directories of the venv
   are now passed to it by name. They come after the tree under test on
   ``sys.path`` and their ``.pth`` files are not processed.

2.2.1:

 * ``ppt sign`` regenerates ``SHA256SUMS`` and ``SHA256SUMS.asc`` when the
   signature on the release does not sign the list, as after an upload that
   stopped between the two files, and when the list differs in any byte from
   the one it would write. A list that is not UTF-8 no longer stops
   ``ppt sign --all``.
 * ``ppt sign --sigstore-only`` never writes a ``SHA256SUMS`` next to an
   existing ``SHA256SUMS.asc``, and keeps a list only when that signature
   signs it, which needs only the public key. Otherwise it fails and says to
   rerun without ``--sigstore-only``. When the asset names already show it
   will refuse, it does so before dispatching ``sign.yml``, and
   ``--dry-run`` prints the refusal.
 * ``ppt sign`` with a single tag signs a version that the PyPI project JSON
   does not list yet, right after an upload, by asking PyPI for that
   version itself. ``--all`` still uses the project JSON alone.
 * ``templates/sign.yml`` takes a ``version`` input, and ``ppt sign`` passes
   the PyPI version it matched, so a tag in another form than the version
   (``v4-2-2`` for ``4.2.2``) no longer fails the run. The tag and version
   inputs accept ``_`` as well. A repository whose ``sign.yml`` predates the
   input gets an error saying to copy the new template.
 * ``ppt backfill`` reads ``SHA256SUMS`` strictly. A list that is not UTF-8
   or has a malformed line fails the resume of a rebuilt release with that
   reason, where a line with a non-hex digest let it go on before.
 * ``VERIFY.md`` names the footer sentence of a release whose bundles all
   came from CI.

2.2.0:

 * ``ppt sign`` takes the Sigstore bundles from the ``sign.yml`` workflow of
   the repository instead of a browser login. It exits 3 when the repository
   has no ``sign.yml`` yet, and ``--sigstore-browser`` signs with the browser
   login as before.
 * ``templates/sign.yml``: the workflow to copy into ``.github/workflows``.
 * ``ppt verify-release`` checks who signed each Sigstore bundle and verifies
   ``SHA256SUMS.asc`` with GPG. Checking that signature needs Rick's public
   key, fingerprint ``149325FD15904E9C4EB89E95E81444E9CE1F695D``, in the
   local keyring.
 * ``VERIFY.md``: how to check a bundle signed by CI.

2.1.0:

 * ``ppt inventory``: PyPI versions against GitHub tags, releases and
   signatures.
 * ``ppt sign``: GPG and Sigstore signatures for the PyPI files of a release.
 * ``ppt match-tag``: find the commit that matches a PyPI sdist and create a
   signed tag for it.
 * ``ppt backfill``: tag, release and sign every PyPI version.
 * ``ppt verify-release`` checks the signatures (``--skip signatures`` to
   leave it out).
 * ``VERIFY.md``: how to verify a signed release.
 * The shared ``common`` module for subprocess and HTTP access.

2.0.0:

 * Fresh start. Modern package, ``ppt`` CLI, release scripts moved here from
   the release-python-package skill.
