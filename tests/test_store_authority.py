"""Manufacturing choices persist independently of native PCB assembly fields."""

from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

from .wx_harness import load, package_stubs, wx_stubs


class Footprint:
    """Keep native data mutable while identifying a physical footprint durably."""

    def __init__(self, reference: str = "R1", uuid: str = "uuid-1") -> None:
        self.reference, self.uuid = reference, uuid
        self.value, self.package, self.lcsc = "10k", "R_0603", "C100"
        self.attributes, self.dnp, self.pads = 0, False, [object(), object()]
        self.m_Uuid = SimpleNamespace(AsString=lambda: self.uuid)

    def GetReference(self) -> str:
        """Return the current designator."""
        return self.reference

    def GetValue(self) -> str:
        """Return current design data."""
        return self.value

    def GetFPID(self) -> Any:
        """Return current package information."""
        return SimpleNamespace(GetLibItemName=lambda: self.package)

    def GetFields(self) -> list[Any]:
        """Expose changing native fields without a mutation API."""
        return [SimpleNamespace(GetName=lambda: "LCSC", GetText=lambda: self.lcsc)]

    def GetAttributes(self) -> int:
        """Return BOM/POS flags."""
        return self.attributes

    def IsDNP(self) -> bool:
        """Return native DNP."""
        return self.dnp

    def Pads(self) -> list[object]:
        """Return live geometric pad data."""
        return self.pads


class Board:
    """Expose saved board identity and mutable footprint membership."""

    def __init__(self, path: Path, footprints: list[Footprint]) -> None:
        self.path, self.footprints = path, footprints

    def GetFileName(self) -> str:
        """Return the saved PCB path."""
        return str(self.path)

    def GetFootprints(self) -> list[Footprint]:
        """Return the current board objects."""
        return self.footprints


@pytest.fixture
def context(tmp_path: Path) -> Any:
    """Use real Store construction and SQLite for every transition."""
    package = "_store_authority_tests"
    module = load(package, "store", {**package_stubs(package), **wx_stubs()})
    fp = Footprint()
    board = Board(tmp_path / "board.kicad_pcb", [fp])
    board.path.touch()
    parent = SimpleNamespace(settings={})
    return SimpleNamespace(
        module=module,
        fp=fp,
        board=board,
        parent=parent,
        open=lambda: module.Store(parent, str(tmp_path), board),
    )


@pytest.mark.parametrize("lcsc", ["", "C999"])
def test_schematic_updates_do_not_replace_existing_choices(
    context: Any, lcsc: str
) -> None:
    """Reopening after native field/flag replacement retains plugin decisions."""
    store = context.open()
    store.set_lcsc_assignments([("R1", "C200", None)])
    context.fp.lcsc, context.fp.attributes, context.fp.dnp = lcsc, 12, True
    context.fp.value, context.fp.package = "20k", "R_0805"
    context.fp.pads.append(object())
    part = context.open().get_part("R1")
    assert part["lcsc"] == "C200"
    assert (
        part["lcsc"],
        part["exclude_from_bom"],
        part["exclude_from_pos"],
        part["is_dnp"],
    ) == ("C200", 0, 0, 0)
    assert (part["value"], part["footprint"], part["pad_count"]) == ("20k", "R_0805", 3)


def test_explicit_clear_stays_blank_after_reopen(context: Any) -> None:
    """Native populated fields cannot refill a deliberate plugin blank."""
    context.open().set_lcsc_assignments([("R1", "", None)])
    assert context.open().get_part("R1")["lcsc"] == ""


@pytest.mark.parametrize("flags", [0, 4, 8, 12])
@pytest.mark.parametrize("dnp", [False, True])
def test_initial_import_flags_and_empty_values(
    context: Any, flags: int, dnp: bool
) -> None:
    """Every false/true combination and blank assignment is real initial data."""
    context.fp.attributes, context.fp.dnp, context.fp.lcsc = flags, dnp, ""
    store = context.open()
    part = store.get_part("R1")
    assert (
        part["lcsc"],
        part["exclude_from_bom"],
        part["exclude_from_pos"],
        part["is_dnp"],
    ) == ("", bool(flags & 8), bool(flags & 4), dnp)
    assert store.new_part_uuids == {"uuid-1"}
    assert context.open().new_part_uuids == set()


def test_uuid_rename_replacement_and_undo(context: Any) -> None:
    """Choices follow a UUID through reference edits and deletion/undo."""
    store = context.open()
    store.set_lcsc_assignments([("R1", "C200", None)])
    context.fp.reference = "R2"
    assert store.get_part("R2")["lcsc"] == "C200"
    assert store.get_part("R1") is None
    context.board.footprints = []
    assert store.read_all() == []
    context.board.footprints = [context.fp]
    assert store.get_part("R2")["lcsc"] == "C200"
    replacement = Footprint("R2", "uuid-new")
    context.board.footprints = [replacement]
    assert store.get_part("R2")["lcsc"] == "C100"
    with pytest.raises(sqlite3.IntegrityError, match="changed"):
        store.set_lcsc_assignments([("R2", "C300", None)], {"R2": "uuid-1"})
    assert store.get_part("R2")["lcsc"] == "C100"


def test_boards_with_matching_uuids_remain_separate(context: Any) -> None:
    """Another PCB in the same directory never inherits the original choices."""
    original = context.open()
    original.set_lcsc_assignments([("R1", "C200", None)])
    second_board = Board(context.board.path.with_name("other.kicad_pcb"), [context.fp])
    second_board.path.touch()
    second = context.module.Store(context.parent, original.project_path, second_board)
    assert second.get_part("R1")["lcsc"] == "C100"
    second.set_lcsc_assignments([("R1", "C300", None)])
    assert original.get_part("R1")["lcsc"] == "C200"
    context.board.path = second_board.path
    with pytest.raises(sqlite3.DatabaseError, match="PCB changed"):
        original.set_lcsc_assignments([("R1", "C400", None)])


def test_moving_the_complete_project_preserves_choices(context: Any) -> None:
    """Identity is relative to project storage, never the old absolute path."""
    store = context.open()
    store.set_lcsc_assignments([("R1", "C200", None)])
    moved = context.board.path.parent / "moved"
    moved.mkdir()
    shutil.move(store.datadir, moved / "jlcpcb")
    context.board.path = moved / context.board.path.name
    reopened = context.module.Store(context.parent, str(moved), context.board)
    assert reopened.get_part("R1")["lcsc"] == "C200"


@pytest.mark.parametrize("invalid", ["missing_uuid", "duplicate_uuid", "duplicate_ref"])
def test_ambiguous_native_identity_is_rejected_before_storage(
    context: Any, invalid: str
) -> None:
    """Never replace persistent identity with a reference or silently merge parts."""
    if invalid == "missing_uuid":
        context.fp.uuid = ""
    else:
        context.board.footprints.append(
            Footprint(
                "R2" if invalid == "duplicate_uuid" else "R1",
                "uuid-1" if invalid == "duplicate_uuid" else "uuid-2",
            )
        )
    with pytest.raises(sqlite3.IntegrityError):
        context.open()
    assert not (context.board.path.parent / "jlcpcb").exists()


@pytest.mark.parametrize("operation", ["assign", "clear", "flags", "missing"])
def test_batch_failures_leave_every_choice_unchanged(
    context: Any, operation: str
) -> None:
    """A later failed update rolls back earlier records in the same action."""
    context.board.footprints.append(Footprint("R2", "uuid-2"))
    store = context.open()
    before = store.read_all()
    with closing(sqlite3.connect(store.dbfile)) as db, db:
        if operation == "missing":
            db.execute("DELETE FROM part_info WHERE footprint_uuid = 'uuid-2'")
        else:
            db.execute(
                "CREATE TRIGGER reject_second BEFORE UPDATE ON part_info WHEN NEW.footprint_uuid = 'uuid-2' BEGIN SELECT RAISE(ABORT, 'second update failed'); END"
            )
    fields = (
        {"exclude_from_bom": 1, "exclude_from_pos": 1, "is_dnp": 1}
        if operation == "flags"
        else {"lcsc": "" if operation == "clear" else "C200"}
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.update_parts({"R1": fields, "R2": fields})
    assert store.read_all() == before
    assert store.new_part_uuids == {"uuid-1", "uuid-2"}


def test_plugin_flags_drive_bom_and_preserve_native_state(context: Any) -> None:
    """Changing DNP/BOM/POS only writes the database and all reads agree."""
    store = context.open()
    store.update_parts({"R1": {"is_dnp": 1, "exclude_from_pos": 1}})
    assert context.fp.attributes == 0 and not context.fp.dnp
    assert store.read_bom_parts() == []
    store.update_parts({"R1": {"is_dnp": 0}})
    assert store.read_bom_parts()[0]["refs"] == "R1"
    snapshot = store.read_all()
    store.update_parts({"R1": {"exclude_from_bom": 1}})
    assert store.read_bom_parts() == []
    assert store.read_bom_parts(snapshot)[0]["refs"] == "R1"


def legacy_database(context: Any, *, flags: Any = None) -> Path:
    """Create actual earlier-release storage including an unmatched reference."""
    path = context.board.path.parent / "jlcpcb" / "project.db"
    path.parent.mkdir()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE part_info (reference TEXT PRIMARY KEY, value TEXT, footprint TEXT, lcsc TEXT, exclude_from_bom INTEGER, exclude_from_pos INTEGER, assembly_flags TEXT)"
        )
        db.executemany(
            "INSERT INTO part_info VALUES (?, '10k', 'R_0603', ?, 1, 0, ?)",
            [("R1", "C900", flags), ("R9", "C999", None)],
        )
        db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO metadata VALUES ('generation_count', '7')")
        db.execute("CREATE TABLE unrelated (value TEXT)")
        db.execute("INSERT INTO unrelated VALUES ('preserve')")
    return path


@pytest.mark.parametrize("flags,expected_dnp", [(None, 1), ('{"is_dnp": false}', 0)])
def test_legacy_upgrade_preserves_decisions_without_archive(
    context: Any, flags: Any, expected_dnp: int
) -> None:
    """DNP comes from recorded flags, or once from the board if historically absent."""
    path = legacy_database(context, flags=flags)
    context.fp.dnp = True
    store = context.open()
    part = store.get_part("R1")
    assert (
        part["lcsc"],
        part["exclude_from_bom"],
        part["exclude_from_pos"],
        part["is_dnp"],
    ) == ("C900", 1, 0, expected_dnp)
    assert store.new_part_uuids == set()
    assert store.get_generation_count() == 7
    assert store.increment_generation_count() == 8
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT * FROM part_info").fetchall() == [
            ("board.kicad_pcb", "uuid-1", "C900", 1, 0, expected_dnp)
        ]
        assert not db.execute("PRAGMA table_info(part_info_legacy)").fetchall()
        assert not db.execute(
            "SELECT value FROM metadata WHERE key = 'part_info_legacy_owner'"
        ).fetchall()
        assert db.execute("SELECT * FROM unrelated").fetchone() == ("preserve",)
    context.fp.lcsc = "C700"
    assert context.open().get_part("R1")["lcsc"] == "C900"


@pytest.mark.parametrize("invalid_schema", [False, True])
def test_existing_archive_cleanup_is_transactional(
    context: Any, invalid_schema: bool
) -> None:
    """Earlier PR archives disappear only when opening the current store succeeds."""
    store = context.open()
    store.set_lcsc_assignments([("R1", "C200", None)])
    with closing(sqlite3.connect(store.dbfile)) as db, db:
        db.execute("CREATE TABLE part_info_legacy (reference TEXT, lcsc TEXT)")
        db.execute("INSERT INTO part_info_legacy VALUES ('R1', 'C900')")
        db.execute(
            "INSERT INTO metadata VALUES ('part_info_legacy_owner', 'board.kicad_pcb')"
        )
        if invalid_schema:
            db.execute("ALTER TABLE part_info ADD COLUMN unexpected TEXT")
    if invalid_schema:
        with pytest.raises(sqlite3.DatabaseError, match="Unsupported"):
            context.open()
    else:
        assert context.open().get_part("R1")["lcsc"] == "C200"
        assert context.open().get_part("R1")["lcsc"] == "C200"
    with closing(sqlite3.connect(store.dbfile)) as db:
        assert (
            bool(db.execute("PRAGMA table_info(part_info_legacy)").fetchall())
            == invalid_schema
        )
        assert (
            bool(
                db.execute(
                    "SELECT value FROM metadata WHERE key = 'part_info_legacy_owner'"
                ).fetchall()
            )
            == invalid_schema
        )


@pytest.mark.parametrize("reason", ["boards", "package"])
def test_ambiguous_legacy_upgrade_requires_acceptance(
    context: Any, reason: str
) -> None:
    """Cancellation preserves old schema/data; accepting retries the same upgrade."""
    path = legacy_database(context)
    if reason == "boards":
        context.board.path.with_name("other.kicad_pcb").touch()
    else:
        context.fp.package = "R_0805"
    calls = []
    context.parent.confirm_part_info_migration = (
        lambda board, reasons: calls.append((board, reasons)) or False
    )
    with pytest.raises(sqlite3.DatabaseError, match="cancelled"):
        context.open()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM part_info").fetchone() == (2,)
        assert not db.execute("PRAGMA table_info(part_info_legacy)").fetchall()
    assert calls[0][0] == "board.kicad_pcb" and calls[0][1]
    context.parent.confirm_part_info_migration = lambda *_: True
    assert context.open().get_part("R1")["lcsc"] == "C900"


def test_upgrade_ddl_and_data_roll_back_together(context: Any) -> None:
    """A failure after converting one row restores the original table and all rows."""
    path = legacy_database(context)
    context.board.footprints.append(Footprint("R9", "uuid-9"))
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE part_info SET exclude_from_bom = 4 WHERE reference = 'R9'")
    with pytest.raises(sqlite3.DatabaseError, match="invalid"):
        context.open()
    with closing(sqlite3.connect(path)) as db, db:
        assert db.execute(
            "SELECT reference, value, footprint, lcsc, exclude_from_bom FROM part_info ORDER BY reference"
        ).fetchall() == [
            ("R1", "10k", "R_0603", "C900", 1),
            ("R9", "10k", "R_0603", "C999", 4),
        ]
        assert not db.execute("PRAGMA table_info(part_info_legacy)").fetchall()
        db.execute("UPDATE part_info SET exclude_from_bom = 1")
    assert context.open().get_part("R1")["lcsc"] == "C900"


def test_supplier_metadata_follows_stored_code_and_isolates_late_results(
    context: Any,
) -> None:
    """Supplier classification cannot attach to stale native or old assigned codes."""
    metadata = {"C100": {"assembly_process": "SMT", "component_product_type": 0}}
    context.parent.library = SimpleNamespace(
        get_lcsc_metadata=lambda _: metadata,
    )
    store = context.open()
    assert store.get_assembly_enrichment_targets() == {}
    store.set_lcsc_assignments([("R1", "C200", None)])
    assert store.get_assembly_enrichment_targets() == {"C200": ["R1"]}
    metadata["C100"] = {"assembly_process": "THT", "component_product_type": 1}
    assert store.get_assembly_enrichment_targets() == {"C200": ["R1"]}
    metadata["C200"] = {"assembly_process": "SMT", "component_product_type": 2}
    assert store.get_assembly_enrichment_targets() == {}
    assert store.get_part("R1")["component_product_type"] == 2


def test_standalone_board_supports_durable_store_identity(
    context: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented standalone provider constructs and reopens real storage."""
    package = "_standalone_store_tests"
    standalone = load(package, "standalone_impl", package_stubs(package))
    monkeypatch.chdir(tmp_path)
    board = standalone.BoardStub()
    store = context.module.Store(context.parent, str(tmp_path), board)
    store.set_lcsc_assignments([("R1", "C200", None)])
    assert board.FindFootprintByReference("R1") is board.GetFootprints()[0]
    assert board.FindFootprintByReference("missing") is None
    reopened = context.module.Store(
        context.parent, str(tmp_path), standalone.BoardStub()
    )
    assert reopened.get_part("R1")["lcsc"] == "C200"
