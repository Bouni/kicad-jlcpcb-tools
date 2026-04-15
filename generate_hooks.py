"""Helpers for running optional generation hooks."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
import subprocess
from typing import Any


@dataclass
class HookRunResult:
    """Result from executing a configured hook script."""

    succeeded: bool = False
    command: list[str] = field(default_factory=list)
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    error_message: str | None = None


def parse_hook_timeout(timeout_seconds: Any) -> int:
    """Parse timeout value from settings and clamp to a sane minimum."""
    try:
        return max(1, int(timeout_seconds))
    except (ValueError, TypeError):
        return 30


def format_hook_error(result: HookRunResult, max_output_chars: int = 2000) -> str:
    """Build a readable error message from a hook execution result."""
    parts = []
    if result.error_message:
        parts.append(result.error_message)
    else:
        parts.append(f"Hook exited with code {result.returncode}.")

    output = (result.stderr or result.stdout).strip()
    if output:
        parts.append(f"Output:\n{output[:max_output_chars]}")
    return "\n\n".join(parts)


def run_hook_script(
    script_path: str,
    timeout_seconds: int,
    env_updates: dict[str, str],
    working_dir: str,
) -> HookRunResult:
    """Run a hook script with timeout and environment updates."""
    normalized_path = script_path.strip()
    if not normalized_path:
        return HookRunResult(succeeded=True, returncode=0)

    if not os.path.isfile(normalized_path):
        return HookRunResult(
            command=[normalized_path],
            error_message=f"Hook script does not exist: {normalized_path}",
        )

    timeout = max(1, int(timeout_seconds))
    command = [normalized_path]
    env = os.environ.copy()
    env.update(env_updates)
    result = HookRunResult(command=command)

    try:
        completed = subprocess.run(
            command,
            check=False,
            cwd=working_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        result.succeeded = completed.returncode == 0
        result.returncode = completed.returncode
        result.stdout = completed.stdout
        result.stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        if isinstance(exc.stdout, str):
            result.stdout = exc.stdout
        if isinstance(exc.stderr, str):
            result.stderr = exc.stderr
        result.error_message = (
            f"Hook timed out after {timeout} seconds: {normalized_path}"
        )
    except OSError as exc:
        result.error_message = f"Failed to execute hook script: {exc}"

    return result


def run_configured_hook(
    stage: str,
    hooks_settings: dict[str, object],
    env_updates: dict[str, str],
    working_dir: str,
    logger: logging.Logger | None = None,
) -> HookRunResult:
    """Run a pre/post hook from settings and return a structured result."""
    script_path = str(hooks_settings.get(f"{stage}_script", "")).strip()
    if not script_path:
        return HookRunResult(succeeded=True, returncode=0)

    timeout_seconds = parse_hook_timeout(hooks_settings.get("timeout_seconds", 30))
    result = run_hook_script(
        script_path=script_path,
        timeout_seconds=timeout_seconds,
        env_updates=env_updates,
        working_dir=working_dir,
    )

    if logger is not None:
        logger.info("Ran %s-generate hook: %s", stage, script_path)
        if result.stdout.strip():
            logger.info("%s hook stdout:\n%s", stage, result.stdout.strip())
        if result.stderr.strip():
            logger.warning("%s hook stderr:\n%s", stage, result.stderr.strip())

    return result
