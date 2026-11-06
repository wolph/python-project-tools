"""The Sigstore login URL relay."""

from __future__ import annotations

import os
import pathlib
import shlex
import subprocess
import sys

import pytest

from python_project_tools import browser_relay, signing

URL: str = 'https://oauth2.sigstore.dev/auth/auth?spam=eggs&state=1'


def test_main_prints_the_url_and_opens_it_without_the_relay(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[tuple[str, str | None]] = []

    def fake_open(url: str) -> bool:
        opened.append((url, os.environ.get('BROWSER')))
        return True

    monkeypatch.setenv('BROWSER', 'relay %s')
    monkeypatch.setattr(browser_relay.webbrowser, 'open', fake_open)
    assert browser_relay.main([URL]) == 0
    assert capsys.readouterr().err == f'ppt: Sigstore login URL: {URL}\n'
    # The real browser is opened with BROWSER gone, so no loop.
    assert opened == [(URL, None)]


def test_main_reports_a_browser_that_did_not_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser_relay.webbrowser, 'open', lambda url: False)
    assert browser_relay.main([URL]) == 1


def test_main_needs_exactly_one_url() -> None:
    assert browser_relay.main([]) == 2
    assert browser_relay.main([URL, URL]) == 2


def test_webbrowser_runs_the_relay_browser_value(
    tmp_path: pathlib.Path,
) -> None:
    """Python's own webbrowser module, given our BROWSER value, runs us.

    The relay's real browser step is replaced by a BROWSER that only
    records the URL, so no tab opens during the test.
    """
    record: pathlib.Path = tmp_path / 'opened'
    recorder: pathlib.Path = tmp_path / 'record.py'
    recorder.write_text(
        'import pathlib, sys\n'
        f'pathlib.Path({str(record)!r}).write_text(sys.argv[1])\n',
        newline='\n',
    )
    script: str = (
        'import os, webbrowser\n'
        "os.environ['BROWSER'] = os.environ['PPT_TEST_BROWSER']\n"
        f'webbrowser.open({URL!r})\n'
    )
    relay: str = signing.relay_browser()
    # The relay hands the URL on to the recorder instead of a real browser.
    following: str = (
        f'{shlex.quote(sys.executable)} {shlex.quote(str(recorder))} %s'
    )
    env: dict[str, str] = {
        **os.environ,
        'PPT_TEST_BROWSER': relay,
        browser_relay.NEXT_BROWSER_ENV: following,
        'PYTHONPATH': os.pathsep.join(sys.path),
    }
    done: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, '-c', script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert f'ppt: Sigstore login URL: {URL}' in done.stderr
    assert record.read_text() == URL
