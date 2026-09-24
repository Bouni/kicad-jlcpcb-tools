"""Verify native feature lane selection and retained failure evidence."""

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_lane(
    directory: Path,
    lane: str = "native-kicad-10",
    *,
    status: int = 0,
    environment: Optional[dict[str, str]] = None,
) -> Any:
    """Run the real dispatcher with labeled command stand-ins, retaining its shell behavior."""
    directory.mkdir(parents=True, exist_ok=True)
    binaries = directory / "bin"
    binaries.mkdir()
    scripts = {
        "id": "echo 1001\n",
        "dpkg": "echo amd64\n",
        "timeout": (
            'printf "%s\\n" "$1" >> "$STUB_ROOT/timeouts"\n'
            'if [ "$STUB_STATUS" -ge 124 ]; then\n'
            ' echo "native stdout"; echo "native stderr" >&2; exit "$STUB_STATUS"\n'
            'fi\nshift\nexec "$@"\n'
        ),
        "xvfb-run": 'export DISPLAY=:lane-fixture\nshift 3\nexec "$@"\n',
        "openbox": 'touch "$STUB_ROOT/wm-ready"\nexec sleep 60\n',
        "xprop": (
            '[ -f "$STUB_ROOT/wm-ready" ] || exit 1\n'
            'echo "_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0x123"\n'
        ),
    }
    for name, body in scripts.items():
        path = binaries / name
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)
    native = directory / ".venv/bin/python"
    native.parent.mkdir(parents=True)
    native.write_text(
        f"#!{sys.executable}\n"
        "import json,os,pathlib,sys\n"
        "args=sys.argv[1:]\n"
        "if args[:2]==['-m','pip']: sys.exit(0)\n"
        "root=pathlib.Path(os.environ['STUB_ROOT'])\n"
        "if args[0]=='scripts/linux_native_impedance_smoke.py':\n"
        " assert os.environ.get('DISPLAY')==':lane-fixture', 'Smoke needs the outer display'\n"
        " assert (root/'wm-ready').is_file(), 'Smoke started before its window manager'\n"
        "with (root/'calls.jsonl').open('a') as log: log.write(json.dumps(args)+'\\n')\n"
        "for flag in ('--artifacts','--output'):\n"
        " if flag in args:\n"
        "  output=pathlib.Path(args[args.index(flag)+1]);output.mkdir(exist_ok=True)\n"
        "  (output/'command.json').write_text(json.dumps(args))\n"
        "proof='tests/test_impedance_source_evidence_native.py' in args\n"
        "probe='scripts/native_impedance_source_probe.py' in args\n"
        "print('proof output' if proof else 'probe output' if probe else 'native stdout')\n"
        "if not proof and not probe: print('native stderr',file=sys.stderr)\n"
        "sys.exit(int(os.environ['PROOF_EXIT' if proof else 'PROBE_EXIT' if probe else 'STUB_STATUS']))\n",
        encoding="utf-8",
    )
    native.chmod(0o755)
    artifacts = directory / ".cache/native-tests" / lane
    artifacts.mkdir(parents=True)
    values = dict(os.environ)
    for name in (
        "NATIVE_GENERATE_BENCHMARK",
        "NATIVE_SMOKE_BENCHMARK",
        "NATIVE_SOURCE_SNAPSHOT_PROBE",
    ):
        values[name] = "0"
    values.update(
        PATH=str(binaries) + os.pathsep + values["PATH"],
        STUB_ROOT=str(directory),
        STUB_STATUS=str(status),
        PROOF_EXIT="0",
        PROBE_EXIT="0",
    )
    values.update(environment or {})
    process = subprocess.run(
        [
            "bash",
            "-c",
            '/usr/bin/python3() { :; }; source "$1" --inside "$2" amd64',
            "lane-fixture",
            str(ROOT / "scripts/run_test_lane.sh"),
            lane,
        ],
        cwd=directory,
        env=values,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    calls = directory / "calls.jsonl"
    timeouts = directory / "timeouts"
    return SimpleNamespace(
        process=process,
        calls=[json.loads(line) for line in calls.read_text().splitlines()]
        if calls.exists()
        else [],
        timeouts=timeouts.read_text().splitlines() if timeouts.exists() else [],
        artifacts=artifacts,
    )


@pytest.mark.parametrize("major", ["7"])
@pytest.mark.parametrize("status", [0, 1, 2, 124, 137])
def test_native_log_capture_preserves_test_runner_and_timeout_failures(
    tmp_path: Path, major: str, status: int
) -> None:
    """Retain complete logs and a failed lane through pipelines and external-output cleanup."""
    run = run_lane(tmp_path, f"native-kicad-{major}", status=status)
    assert run.process.returncode == status, run.process.stderr
    assert run.timeouts == ["900s"]
    log = "pytest.log"
    lines = (run.artifacts / log).read_text().splitlines()
    assert sorted(lines) == ["native stderr", "native stdout"]
    assert not run.process.stderr
    assert bool(run.calls) == (status < 124)
