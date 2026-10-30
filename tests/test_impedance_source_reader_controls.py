"""Fresh source proof surrounds owner-scoped toolbar snapshot materialization."""

from dataclasses import replace
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from .test_impedance_controls import _configuration, _snapshot, harness
from .test_schematic_autosave_close import CloseEvent, window

__all__ = ["harness", "window"]


@pytest.fixture
def reader_api(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    """Keep real controls/readers while observing complete-evidence boundaries."""
    package = harness.integration.__package__
    adapter = importlib.import_module(f"{package}.pcbnew_adapter")
    evidence = importlib.import_module(f"{package}.source_evidence")
    state = SimpleNamespace(
        builds=[], proofs=[], guards=[], error=None, guard_error=None, action=None
    )
    harness.board.version = "initial"
    harness.board.this = 101

    def guarded_board() -> Any:
        """Represent the parent's allocation and unreliable-board checks."""
        state.guards.append(harness.board)
        if state.guard_error is not None:
            raise state.guard_error
        return harness.board

    def acquire(board: Any, _module: Any, *, netclass_source: Any = None) -> Any:
        """Read mutable source state afresh, independently of Python snapshots."""
        state.proofs.append((board, netclass_source))
        if state.error is not None:
            raise state.error
        values = (board.version, getattr(netclass_source, "version", None))
        return evidence.SourceEvidence(repr(values).encode("ascii"))

    def build(board: Any, _module: Any, *, netclass_source: Any = None) -> Any:
        """Construct a distinct immutable result on every actual materialization."""
        state.builds.append((board, netclass_source))
        result = replace(
            _snapshot(harness),
            context_digest=repr(
                (board.version, getattr(netclass_source, "version", None))
            ),
        )
        if state.action is not None:
            state.action()
        return result

    harness.parent._legacy_board = guarded_board
    monkeypatch.setattr(evidence, "acquire_source_evidence", acquire)
    monkeypatch.setattr(adapter, "snapshot_board", build)
    controls = harness.integration.ImpedanceControls(harness.parent, harness.toolbar)
    return SimpleNamespace(
        controls=controls, state=state, native=harness, adapter=adapter
    )


def test_unchanged_live_reads_reuse_materialization_with_fresh_proof(
    reader_api: SimpleNamespace,
) -> None:
    """Each boundary checks evidence, while one completed snapshot is retained."""
    api = reader_api
    first = api.controls._live_snapshot()
    for _boundary in range(4):
        assert api.controls._live_snapshot() is first
    assert len(api.state.builds) == 1
    assert len(api.state.proofs) == 6  # Cold pre/post proof, then four fresh hits.
    assert len(api.state.guards) >= 5


def test_nonvariant_live_reads_apply_the_parent_identity_guard(
    reader_api: SimpleNamespace,
) -> None:
    """An unreliable or replaced live board is rejected before evidence reads."""
    api = reader_api
    api.state.guard_error = ValueError("board was replaced")
    with pytest.raises(ValueError, match="board was replaced"):
        api.controls._live_snapshot()
    assert api.state.proofs == [] and api.state.builds == []


def test_live_evidence_changes_and_failed_reads_cannot_reuse_old_source(
    reader_api: SimpleNamespace,
) -> None:
    """Recovery after a failed proof starts cold, even if old evidence returns."""
    api = reader_api
    first = api.controls._live_snapshot()
    api.native.board.version = "edited"
    second = api.controls._live_snapshot()
    assert second is not first and second != first
    assert len(api.state.builds) == 2
    api.state.error = RuntimeError("native proof failed")
    with pytest.raises(RuntimeError, match="native proof failed"):
        api.controls._live_snapshot()
    api.state.error = None
    recovered = api.controls._live_snapshot()
    assert recovered == second and recovered is not second
    assert len(api.state.builds) == 3


def test_failed_parent_guard_discards_the_warm_live_slot(
    reader_api: SimpleNamespace,
) -> None:
    """A lifetime failure cannot leave last-good state available on recovery."""
    api = reader_api
    first = api.controls._live_snapshot()
    api.state.guard_error = ValueError("board is unreliable")
    with pytest.raises(ValueError, match="board is unreliable"):
        api.controls._live_snapshot()
    assert api.controls._live_source_reader._snapshot is None
    api.state.guard_error = None
    recovered = api.controls._live_snapshot()
    assert recovered == first and recovered is not first
    assert len(api.state.builds) == 2


@pytest.mark.parametrize("action", ["close", "invalidate", "nested"])
def test_live_acquisition_checks_lifetime_before_further_native_guards(
    reader_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """Superseded live proof cannot execute a subsequent native currentness check."""
    api = reader_api
    api.controls._live_snapshot()
    evidence = importlib.import_module(
        api.native.integration.__package__ + ".source_evidence"
    )
    acquire = evidence.acquire_source_evidence
    nested: list[Any] = []
    fired = False
    guards_after_callback = 0

    def supersede(current: Any, module: Any, **kwargs: Any) -> Any:
        """Keep the old evidence but supersede its owning read before return."""
        nonlocal fired, guards_after_callback
        proof = acquire(current, module, **kwargs)
        if not fired:
            fired = True
            if action == "close":
                api.controls.close()
            elif action == "invalidate":
                api.controls._live_source_reader.invalidate()
            else:
                nested.append(api.controls._live_snapshot())
            guards_after_callback = len(api.state.guards)
        return proof

    monkeypatch.setattr(evidence, "acquire_source_evidence", supersede)
    with pytest.raises(evidence.SourceSnapshotChanged, match="owner changed"):
        api.controls._live_snapshot()
    assert len(api.state.guards) == guards_after_callback
    if nested:
        assert api.controls._live_source_reader._snapshot is nested[0]
    else:
        assert api.controls._live_source_reader._snapshot is None


def test_live_owner_key_distinguishes_replacement_but_not_native_proxy_aliases(
    reader_api: SimpleNamespace,
) -> None:
    """One SWIG allocation can have aliases; reopening the same path is distinct."""
    api = reader_api
    first = api.controls._live_snapshot()
    api.native.board = SimpleNamespace(**vars(api.native.board))
    assert api.controls._live_snapshot() is first
    api.native.board.this = 202
    replacement = api.controls._live_snapshot()
    assert replacement == first and replacement is not first
    assert len(api.state.builds) == 2


@pytest.mark.parametrize("change", ["close", "replacement", "invalid_variant"])
def test_renderer_constructor_cannot_outlive_real_source_controls(
    reader_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    change: str,
) -> None:
    """An unchanged capture epoch cannot authorize use of a closed/replaced board."""
    api = reader_api
    native = api.native
    captures = native.integration.CaptureSession(api.controls, tmp_path)
    snapshot = captures.snapshot()
    section = native.matching.analyze(_configuration(native), snapshot).sections[0]
    render = importlib.import_module(f"{native.integration.__package__}.render")
    original = render.SectionRenderer
    after_close: list[int] = []

    def construct(*args: Any, **kwargs: Any) -> Any:
        """Change the actual source owner without invalidating the capture session."""
        candidate = original(*args, **kwargs)
        if change == "close":
            api.controls.close()
            after_close.append(len(api.state.guards))
        elif change == "replacement":
            native.board = SimpleNamespace(**vars(native.board))
            native.board.this = 202
        else:
            native.parent._variant_controller = SimpleNamespace(
                session=SimpleNamespace(output_variant=123)
            )
        return candidate

    monkeypatch.setattr(render, "SectionRenderer", construct)
    try:
        with pytest.raises(
            (native.model.ValidationError, api.adapter.BoardSnapshotError),
            match="changed|closed|Select",
        ):
            captures.preview(section)
        assert captures.renderer is None
        assert not native.renders and not captures._pending_owned
        assert len(native.renderers) == 1
        assert native.renderers[0].closed and native.renderers[0].close_calls == 1
        if after_close:
            assert len(api.state.guards) == after_close[0]
    finally:
        captures.close()


def _output(api: SimpleNamespace, name: str = "A") -> Any:
    """Select a fabrication output without changing the live editor source."""
    api.native.parent._variant_controller = SimpleNamespace(
        session=SimpleNamespace(output_variant=name)
    )
    return SimpleNamespace(
        version="output",
        this=303,
        GetFileName=lambda: api.native.board_path,
        GetCurrentVariant=lambda: name,
    )


def test_output_reader_reuses_final_variant_snapshot_and_checks_live_metadata(
    reader_api: SimpleNamespace,
) -> None:
    """The cached object includes variant hashing and fresh live class metadata."""
    api = reader_api
    board = _output(api)
    first = api.controls.snapshot_output_board(board)
    assert api.controls.snapshot_output_board(board) is first
    assert len(api.state.builds) == 1
    assert api.state.builds[0][1] is api.native.board
    api.native.board.version = "different native classes"
    changed = api.controls.snapshot_output_board(board)
    assert changed is not first and changed != first
    assert len(api.state.builds) == 2
    api.native.parent._variant_controller.session.output_variant = "B"
    with pytest.raises(ValueError, match="output variant changed"):
        api.controls.snapshot_output_board(board)


def test_output_selection_changed_during_build_cannot_publish_old_snapshot(
    reader_api: SimpleNamespace,
) -> None:
    """The post-build proof checks selection again before returning a candidate."""
    api = reader_api
    board = _output(api)

    def change_selection() -> None:
        api.native.parent._variant_controller.session.output_variant = "B"

    api.state.action = change_selection
    with pytest.raises(ValueError, match="output variant changed"):
        api.controls.snapshot_output_board(board)


def test_output_owner_close_invalidates_reader_before_native_release(
    reader_api: SimpleNamespace,
) -> None:
    """Dependent capture state and snapshot bytes clear before native destruction."""
    api = reader_api
    _output(api)
    steps: list[str] = []

    class Owner:
        """Expose native lifetime ordering independently of its Python wrapper."""

        def __init__(self, name: str) -> None:
            self.board = SimpleNamespace(
                version="output",
                this=303,
                GetFileName=lambda: api.native.board_path,
                GetCurrentVariant=lambda: name,
            )
            self.closed = False

        def close(self) -> None:
            assert api.controls._output_source_reader._snapshot is None
            self.closed = True
            steps.append("native-close")

    def invalidate() -> None:
        assert api.controls._output_source_reader._snapshot is None
        steps.append("capture-invalidate")

    api.native.parent.fabrication = SimpleNamespace(copy_variant_board=Owner)
    output = api.native.integration.OutputBoardSession(api.controls, invalidate)
    output.snapshot()
    owner = output.owner
    assert api.controls._output_source_reader._snapshot is not None
    steps.clear()
    output.close()
    assert owner.closed and output.owner is None
    assert steps == ["capture-invalidate", "native-close"]


@pytest.mark.parametrize("nested_revision", [False, True])
def test_failed_output_preparation_clears_only_its_own_published_slot(
    reader_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    nested_revision: bool,
) -> None:
    """Failed preparation releases its allocation without discarding newer work."""
    api = reader_api
    _output(api)
    closed_slots: list[Any] = []
    owners: list[Any] = []
    nested: list[Any] = []

    class Owner:
        """Observe reader ownership at the exact native destruction boundary."""

        def __init__(self, name: str) -> None:
            self.board = SimpleNamespace(
                version="output",
                this=303 + len(owners),
                GetFileName=lambda: api.native.board_path,
                GetCurrentVariant=lambda: name,
            )
            self.close_calls = 0
            owners.append(self)

        def close(self) -> None:
            self.close_calls += 1
            closed_slots.append(api.controls._output_source_reader._snapshot)

    api.native.parent.fabrication = SimpleNamespace(copy_variant_board=Owner)
    source_revision = api.controls._source_revision
    reads = 0

    def fail_after_output_publication() -> Any:
        """Fail the final live check after a completed detached snapshot read."""
        nonlocal reads
        reads += 1
        if reads == 2:
            if nested_revision:
                replacement = api.native.integration.OutputBoardSession(
                    api.controls, lambda: None
                )
                nested.append(replacement)
                nested.append(replacement.snapshot())
            raise ValueError("final live verification failed")
        return source_revision()

    monkeypatch.setattr(api.controls, "_source_revision", fail_after_output_publication)
    output = api.native.integration.OutputBoardSession(api.controls, lambda: None)
    try:
        with pytest.raises(ValueError, match="final live verification failed"):
            output.snapshot()
        assert owners[0].close_calls == 1
        expected = nested[1] if nested_revision else None
        assert len(closed_slots) == 1 and closed_slots[0] is expected
        assert api.controls._output_source_reader._snapshot is expected
    finally:
        if nested:
            nested[0].close()


def _guarded_output_session(api: SimpleNamespace) -> tuple[Any, list[str]]:
    """Reject every simulated native access once the owning allocation is freed."""
    _output(api)
    dead_reads: list[str] = []
    allocations: list[int] = []

    class Board:
        """Model a borrowed native wrapper whose allocation can disappear reentrantly."""

        def __init__(self, name: str, address: int) -> None:
            self.closed, self.name, self.this = False, name, address
            self.version = "output"

        def __getattribute__(self, name: str) -> Any:
            """Expose use-after-free even before a getter is invoked."""
            native_names = {
                "this",
                "m_Uuid",
                "version",
                "GetFileName",
                "GetCurrentVariant",
            }
            if name in native_names and object.__getattribute__(self, "closed"):
                dead_reads.append(name)
                raise AssertionError("native output getter after deletion")
            return object.__getattribute__(self, name)

        def GetFileName(self) -> str:
            return api.native.board_path

        def GetCurrentVariant(self) -> str:
            return self.name

    class Owner:
        """Invalidate the allocation while leaving the borrowed Python wrapper alive."""

        def __init__(self, name: str) -> None:
            allocations.append(303 + len(allocations))
            self.board = Board(name, allocations[-1])

        def close(self) -> None:
            self.board.closed = True

    api.native.parent.fabrication = SimpleNamespace(copy_variant_board=Owner)
    output = api.native.integration.OutputBoardSession(api.controls, lambda: None)
    output.snapshot()
    return output, dead_reads


@pytest.mark.parametrize("nested_revision", [False, True])
@pytest.mark.parametrize(
    "boundary", ["warm", "cold", "postcheck", "unsupported", "metadata"]
)
def test_output_acquisition_close_rejects_before_any_deleted_native_access(
    reader_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    nested_revision: bool,
    boundary: str,
) -> None:
    """A callback can close or replace output while a fresh proof is acquired."""
    api = reader_api
    output, dead_reads = _guarded_output_session(api)
    board = output.board
    evidence = importlib.import_module(
        api.native.integration.__package__ + ".source_evidence"
    )
    acquire = evidence.acquire_source_evidence
    nested: list[Any] = []
    if boundary in ("cold", "postcheck"):
        api.controls._invalidate_output_source()
    proofs = 0

    def close_or_replace() -> None:
        """Close the old native allocation, optionally publishing a newer result."""
        output.close()
        if nested_revision:
            replacement = api.native.integration.OutputBoardSession(
                api.controls, lambda: None
            )
            nested.append(replacement)
            nested.append(replacement.snapshot())

    def close_after_acquire(current: Any, module: Any, **kwargs: Any) -> Any:
        """Return valid old bytes after closing or replacing their native owner."""
        nonlocal proofs
        proof = acquire(current, module, **kwargs)
        if current is board:
            proofs += 1
            if boundary != "metadata" and (boundary != "postcheck" or proofs == 2):
                close_or_replace()
                if boundary == "unsupported":
                    raise evidence.SourceEvidenceUnavailable("missing capability")
        return proof

    monkeypatch.setattr(evidence, "acquire_source_evidence", close_after_acquire)
    if boundary == "metadata":
        get_live = api.native.parent._legacy_board
        guards = 0

        def close_from_metadata_guard() -> Any:
            """Model invalidation inside the parent check before borrowed getters."""
            nonlocal guards
            live = get_live()
            guards += 1
            if guards == 2:
                close_or_replace()
            return live

        monkeypatch.setattr(
            api.native.parent, "_legacy_board", close_from_metadata_guard
        )
    try:
        with pytest.raises(evidence.SourceSnapshotChanged, match="owner changed"):
            api.controls.snapshot_output_board(board)
        assert dead_reads == []
        if nested_revision:
            assert api.controls._output_source_reader._snapshot is nested[1]
            assert api.controls.snapshot_output_board(nested[0].board) is nested[1]
        else:
            assert api.controls._output_source_reader._snapshot is None
    finally:
        if nested:
            nested[0].close()


def test_nonvariant_report_board_has_a_separate_bounded_reader_slot(
    reader_api: SimpleNamespace,
) -> None:
    """Report reads cannot replace the live memo or retain previous native owners."""
    api = reader_api
    live = api.controls._live_snapshot()
    first_board = SimpleNamespace(
        version="report", this=303, GetFileName=lambda: api.native.board_path
    )
    first = api.controls.snapshot_output_board(first_board)
    assert api.controls.snapshot_output_board(first_board) is first
    second_board = SimpleNamespace(
        version="report", this=404, GetFileName=lambda: api.native.board_path
    )
    second = api.controls.snapshot_output_board(second_board)
    assert second == first and second is not first
    assert api.controls._live_snapshot() is live
    assert api.controls._output_source_reader._snapshot is second
    assert all(metadata is None for _board, metadata in api.state.builds)


def test_controls_close_discards_owned_snapshots_and_rejects_later_reads(
    reader_api: SimpleNamespace,
) -> None:
    """Frame teardown cannot leave a reader available to queued callbacks."""
    api = reader_api
    api.controls._live_snapshot()
    api.controls.close()
    proofs = len(api.state.proofs)
    for _attempt in range(2):
        with pytest.raises(ValueError, match="closed"):
            api.controls._live_snapshot()
    assert len(api.state.proofs) == proofs
    assert api.controls._live_source_reader._snapshot is None
    assert api.controls._output_source_reader._snapshot is None


def test_prepared_source_keeps_original_uncached_failure(
    reader_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A warm live snapshot cannot replace preparation's original failure."""
    api = reader_api
    api.controls._live_snapshot()
    error = RuntimeError("prepared source failed")

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(api.adapter, "snapshot_board", fail)
    prepared = api.controls.prepared_source_revision(api.native.board)
    assert prepared.error is error and prepared.revision is None


def test_accepted_frame_close_releases_readers_after_retryable_save(
    window: Any,
) -> None:
    """A veto leaves source readers usable; successful teardown closes them once."""
    closed: list[bool] = []
    window._impedance = SimpleNamespace(close=lambda: closed.append(window._closing))
    window.export_to_schematic.side_effect = [False, True]
    first = CloseEvent()
    window.quit_dialog(first)
    assert first.vetoed and not closed
    window.quit_dialog(CloseEvent())
    window.quit_dialog(CloseEvent())
    assert closed == [True]
