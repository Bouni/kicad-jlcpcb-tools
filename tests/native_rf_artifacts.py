"""Share only immutable, identical native RF fill/DRC fixture preparations."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
from typing import Optional

import pytest


@dataclass(frozen=True)
class RFSourceFiles:
    """Exact board and adjacent settings, including an absent optional file."""

    name: str
    board: bytes
    project: Optional[bytes]
    rules: Optional[bytes]

    @classmethod
    def read(cls, path: Path) -> "RFSourceFiles":
        """Read every file that can affect the native fixture preparation."""
        project, rules = path.with_suffix(".kicad_pro"), path.with_suffix(".kicad_dru")
        return cls(
            path.name,
            path.read_bytes(),
            project.read_bytes() if project.exists() else None,
            rules.read_bytes() if rules.exists() else None,
        )

    def write_to(self, directory: Path) -> Path:
        """Materialize independent mutable files into an empty directory."""
        directory.mkdir(parents=True, exist_ok=True)
        assert not any(directory.iterdir()), "RF artifact destination must be empty"
        path = directory / self.name
        path.write_bytes(self.board)
        for suffix, contents in (
            (".kicad_pro", self.project),
            (".kicad_dru", self.rules),
        ):
            if contents is not None:
                path.with_suffix(suffix).write_bytes(contents)
        return path


@dataclass(frozen=True)
class PreparedRFArtifacts:
    """Native output bytes; callers independently parse and materialize them."""

    filled: RFSourceFiles
    report: bytes


class NativeRFArtifactStore:
    """Prepare native fixtures without publishing partially generated output."""

    def __init__(
        self, directory: Path, run_native: Callable[[list[str]], None]
    ) -> None:
        """Own temporary cold preparation directories for one pytest session."""
        self.directory = directory
        self.run_native = run_native
        self.preparations = 0
        self._prepared: dict[tuple[str, str, RFSourceFiles], PreparedRFArtifacts] = {}

    def prepare(
        self, source: RFSourceFiles, *, executable: str, version: str
    ) -> PreparedRFArtifacts:
        """Fill and check the exact input files through the real native CLI."""
        key = executable, version, source
        if key in self._prepared:
            return self._prepared[key]
        self.preparations += 1
        directory = self.directory / str(self.preparations)
        path = source.write_to(directory)
        report = directory / "drc.json"
        self.run_native(
            [
                executable,
                "pcb",
                "drc",
                "--refill-zones",
                "--save-board",
                "--format",
                "json",
                "--output",
                str(report),
                str(path),
            ]
        )
        result = PreparedRFArtifacts(RFSourceFiles.read(path), report.read_bytes())
        self._prepared[key] = result
        return result


_SESSION_STORES = pytest.StashKey[
    dict[tuple[str, int, int, int, int, int], tuple[str, NativeRFArtifactStore]]
]()


def prepare_native_rf_board(
    source: RFSourceFiles,
    executable: str,
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    run_native: Callable[[list[str]], None],
) -> PreparedRFArtifacts:
    """Reuse an exact cold native preparation across independent test modules.

    The config owns the store instead of a module's imported fixture definition.
    Outputs never survive the pytest session, and replaced CLI executables receive
    a new store and a freshly queried native version.
    """
    resolved = Path(shutil.which(executable) or executable).resolve(strict=True)
    status = resolved.stat()
    identity = (
        str(resolved),
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )
    stores = request.config.stash.setdefault(_SESSION_STORES, {})
    if identity not in stores:
        version = subprocess.run(
            [str(resolved), "version", "--format", "plain"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        assert version, "Native RF preparation requires an identified KiCad version"
        stores[identity] = (
            version,
            NativeRFArtifactStore(
                tmp_path_factory.mktemp("native-rf-prepared"), run_native
            ),
        )
    version, store = stores[identity]
    return store.prepare(source, executable=str(resolved), version=version)
