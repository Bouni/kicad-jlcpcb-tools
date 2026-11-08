"""Regression tests for fabrication hook diagnostics."""

import locale
from pathlib import Path
import subprocess

import pytest

import generate_hooks


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        (b"building", b"waiting\xc3", ("building", "waiting\ufffd")),
        ("building", "waiting", ("building", "waiting")),
        (None, None, ("", "")),
    ],
)
def test_timeout_preserves_hook_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stdout: bytes | str | None,
    stderr: bytes | str | None,
    expected: tuple[str, str],
) -> None:
    """Keep partial diagnostics even when subprocess returns bytes in text mode."""
    script = tmp_path / "hook"
    script.touch()

    def time_out(command: list[str], *, timeout: int, **_options: object) -> None:
        raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", time_out)
    monkeypatch.setattr(locale, "getpreferredencoding", lambda _setlocale: "utf-8")
    result = generate_hooks.run_hook_script(str(script), 1, {}, str(tmp_path))

    assert not result.succeeded
    assert (result.stdout, result.stderr) == expected
    error = generate_hooks.format_hook_error(result)
    assert "timed out" in error
    _, expected_stderr = expected
    if expected_stderr:
        assert expected_stderr in error
