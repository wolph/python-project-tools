"""Show the Sigstore login URL, then open it in the real browser.

sigstore opens its OIDC login with Python's ``webbrowser`` module, which
honours the BROWSER environment variable. ``signing`` points BROWSER at
this module, so the URL lands on stderr, in whatever log a person or a
watcher reads, before the usual browser tab opens. A login tab that
nobody sees no longer leaves the run waiting with no way to reach it.
"""

from __future__ import annotations

import os
import sys
import webbrowser

URL_NOTICE: str = 'ppt: Sigstore login URL: {url}'
NEXT_BROWSER_ENV: str = 'PPT_RELAY_NEXT_BROWSER'


def main(argv: list[str]) -> int:
    """Print the URL from ``argv`` to stderr and open it, exit 0 on success."""
    if len(argv) != 1:
        print('usage: browser_relay URL', file=sys.stderr)  # noqa: T201
        return 2
    url: str = argv[0]
    print(URL_NOTICE.format(url=url), file=sys.stderr, flush=True)  # noqa: T201
    # Without BROWSER the module picks the platform browser, not us again.
    # NEXT_BROWSER_ENV lets a test put a recorder in that place instead.
    following: str | None = os.environ.pop(NEXT_BROWSER_ENV, None)
    if following:
        os.environ['BROWSER'] = following
    else:
        os.environ.pop('BROWSER', None)
    opened: bool = webbrowser.open(url)
    return 0 if opened else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
