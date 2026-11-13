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
