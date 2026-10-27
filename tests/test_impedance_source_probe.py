"""Verify diagnostic source probes without substituting their native evidence."""

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from .test_impedance_native_ci_selection import run_lane


@pytest.mark.parametrize("proof_exit,probe_exit", [(0, 0), (7, 0), (0, 9), (7, 9)])
def test_manual_source_probe_runs_both_diagnostics_and_preserves_failure(
    tmp_path: Path, proof_exit: int, probe_exit: int
) -> None:
    """A failed native proof still yields timing receipts and a failed job."""
    run = run_lane(
        tmp_path,
        environment={
            "NATIVE_SOURCE_SNAPSHOT_PROBE": "1",
            "PROOF_EXIT": str(proof_exit),
            "PROBE_EXIT": str(probe_exit),
        },
    )
    assert len(run.calls) == 2
    assert "tests/test_impedance_source_evidence_native.py" in run.calls[0]
    assert run.calls[1][0] == "scripts/native_impedance_source_probe.py"
    assert run.timeouts == ["300s", "300s"]
    assert (run.artifacts / "source-pytest.log").read_text() == "proof output\n"
    assert (run.artifacts / "source-probe.log").read_text() == "probe output\n"
    assert (run.process.returncode == 0) == (proof_exit == probe_exit == 0)


def test_source_probe_times_every_fresh_check_without_timing_equality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A warm probe must invoke its reader for every sample and retain receipts."""
    api = importlib.import_module("scripts.native_impedance_source_probe")
    events: list[str] = []
    snapshot = object()
    now = iter((0.0, 2.0, 5.0, 8.0, 10.0, 14.0))
    monkeypatch.setattr(api.time, "perf_counter", lambda: next(now))

    def read() -> Any:
        events.append("fresh read")
        return snapshot

    def check(value: Any) -> None:
        assert value is snapshot
        events.append("check outside timing")

    result = api.measure_reads(read, check, samples=3)
    assert events == ["fresh read", "check outside timing"] * 3
    assert result == {
        "calls": 3,
        "seconds": 9.0,
        "mean_seconds": 3.0,
        "min_seconds": 2.0,
        "max_seconds": 4.0,
    }


@pytest.mark.parametrize("samples", [0, -1, True, 1001])
def test_source_probe_rejects_invalid_samples_without_reading(samples: int) -> None:
    """Invalid profiling requests cannot trigger native work or unbounded runs."""
    api = importlib.import_module("scripts.native_impedance_source_probe")
    with pytest.raises(ValueError, match="samples"):
        api.measure_reads(
            lambda: pytest.fail("unexpected native read"),
            lambda _: None,
            samples=samples,
        )


def test_source_probe_propagates_reader_and_verification_failures() -> None:
    """A failed read cannot become a successful benchmark receipt."""
    api = importlib.import_module("scripts.native_impedance_source_probe")

    def fail() -> Any:
        raise RuntimeError("native read failed")

    with pytest.raises(RuntimeError, match="native read failed"):
        api.measure_reads(fail, lambda _: None, samples=1)
    with pytest.raises(RuntimeError, match="native read failed"):
        api.measure_reads(lambda: object(), lambda _: fail(), samples=1)


@pytest.mark.parametrize("fail", [False, True])
def test_component_profile_observes_extra_reads_and_restores_callbacks(
    fail: bool,
) -> None:
    """Diagnostic wrappers apply only to extra samples and preserve real failures."""
    api = importlib.import_module("scripts.native_impedance_source_probe")
    calls: list[str] = []

    def polygons() -> str:
        calls.append("polygons")
        return "native contour"

    def features() -> str:
        return adapter._copper_polygons()

    adapter = SimpleNamespace(
        _copper_polygons=polygons, _board_settings_records=lambda: "settings"
    )
    source = SimpleNamespace(
        _feature_evidence=features,
        text_context_records=lambda: "text",
        project_context_records=lambda: "project",
        netclass_context=lambda: "classes",
        pairing_context=lambda: "pairs",
    )
    original = source._feature_evidence
    snapshot = object()

    def read() -> Any:
        assert source._feature_evidence is not original
        for owner, name in (
            (source, "_feature_evidence"),
            (adapter, "_board_settings_records"),
            (source, "text_context_records"),
            (source, "project_context_records"),
            (source, "netclass_context"),
            (source, "pairing_context"),
        ):
            getattr(owner, name)()
        if fail:
            raise RuntimeError("diagnostic acquisition failed")
        return snapshot

    def verify(value: Any) -> None:
        assert value is snapshot
        calls.append("verify")

    if fail:
        with pytest.raises(RuntimeError, match="diagnostic acquisition failed"):
            api.profile_acquisition_components(source, adapter, read, verify, samples=2)
    else:
        result = api.profile_acquisition_components(
            source, adapter, read, verify, samples=2
        )
        assert result["acquisition"]["calls"] == 2
        assert result["copper_polygon_calls"] == 2
        assert len(result["components"]) == 6
        assert all(value["calls"] == 2 for value in result["components"].values())
        assert calls == ["polygons", "verify"] * 2
        assert result["diagnostic_only"] is True
        assert "overlap" in result["accounting"]
    assert source._feature_evidence is original
    assert adapter._copper_polygons is polygons


def test_focused_source_profile_preserves_normal_native_and_smoke_coverage(
    tmp_path: Path,
) -> None:
    """The opt-in probe is isolated; ordinary CI retains both existing checks."""
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    assert "      source_snapshot_probe:\n" in workflow
    assert "NATIVE_SOURCE_SNAPSHOT_PROBE:" in workflow
    assert "--env NATIVE_SOURCE_SNAPSHOT_PROBE" in (
        root / "scripts/run_test_lane.sh"
    ).read_text(encoding="utf-8")
    normal = run_lane(tmp_path / "normal")
    assert normal.process.returncode == 0, normal.process.stderr
    assert len(normal.calls) == 1
    assert normal.calls[0][0] == "scripts/native_kicad_runner.py"
    conflict = run_lane(
        tmp_path / "conflict",
        environment={
            "NATIVE_SOURCE_SNAPSHOT_PROBE": "1",
            "NATIVE_SMOKE_BENCHMARK": "1",
        },
    )
    assert conflict.process.returncode == 2
    assert "Select only one manual native profile" in conflict.process.stderr
    assert not conflict.calls


def test_synthetic_spacing_uuid_seed_covers_children_deterministically() -> None:
    """Fixture identity is stable without changing geometry or production policy."""
    from scripts.native_impedance_workflow import seed_native_item_identity

    class Item:
        def __init__(self, children: tuple[Any, ...] = ()) -> None:
            self._uuid = "random"
            self.children = children

        @property
        def m_Uuid(self) -> str:  # noqa: N802 - preserve native read-only API.
            return self._uuid

        def SetUuid(self, value: str) -> None:
            self._uuid = value

        def GraphicalItems(self) -> tuple[Any, ...]:
            return self.children

    native = SimpleNamespace(KIID=lambda value: value)
    first = Item((Item(), Item()))
    second = Item((Item(), Item()))
    seed_native_item_identity(first, native, "route/footprint/0")
    seed_native_item_identity(second, native, "route/footprint/0")
    assert first.m_Uuid == second.m_Uuid != "random"
    assert [item.m_Uuid for item in first.children] == [
        item.m_Uuid for item in second.children
    ]
    assert len({first.m_Uuid, *(item.m_Uuid for item in first.children)}) == 3


def test_reader_receipts_count_every_fresh_check_build_and_failure() -> None:
    """Full builds stay distinct from cheap checks, including failed callbacks."""
    from scripts.native_impedance_workflow import record_source_snapshot_timings

    class Reader:
        def __init__(self) -> None:
            self.statistics = {"hits": 0, "builds": 0, "failures": 0}
            self.cached = None

        def read(
            self, acquire: Any, build: Any, *, owner_key: Any, force: bool = False
        ) -> Any:
            acquire()
            if self.cached is not None and not force:
                self.statistics["hits"] += 1
                return self.cached
            self.statistics["builds"] += 1
            try:
                candidate = build()
            except RuntimeError:
                self.statistics["failures"] += 1
                raise
            acquire()
            self.cached = candidate
            return candidate

    source = SimpleNamespace(
        SourceSnapshotReader=Reader, acquire_source_evidence=lambda: "fresh"
    )
    native_read = Reader.read
    native_acquire = source.acquire_source_evidence
    phases: dict[str, Any] = {}
    reader = Reader()
    snapshot = object()

    def fail() -> Any:
        raise RuntimeError("build failed")

    with record_source_snapshot_timings(phases, source):
        assert (
            reader.read(
                source.acquire_source_evidence, lambda: snapshot, owner_key="owned"
            )
            is snapshot
        )
        assert (
            reader.read(
                source.acquire_source_evidence,
                lambda: pytest.fail("cache hit rebuilt"),
                owner_key="owned",
            )
            is snapshot
        )
        with pytest.raises(RuntimeError, match="build failed"):
            reader.read(
                source.acquire_source_evidence, fail, owner_key="owned", force=True
            )

        def nested_acquire() -> Any:
            reader.read(source.acquire_source_evidence, fail, owner_key="owned")
            return source.acquire_source_evidence()

        assert reader.read(nested_acquire, fail, owner_key="owned") is snapshot
    assert Reader.read is native_read
    assert source.acquire_source_evidence is native_acquire
    assert phases["source_snapshot.read"]["calls"] == 5
    assert phases["source_snapshot.evidence"]["calls"] == 6
    assert phases["source_snapshot.native_evidence"]["calls"] == 6
    assert phases["source_snapshot.build"]["calls"] == 2
    assert phases["source_snapshot.count.hits"]["calls"] == 3
    assert phases["source_snapshot.count.builds"]["calls"] == 2
    assert phases["source_snapshot.count.failures"]["calls"] == 1
