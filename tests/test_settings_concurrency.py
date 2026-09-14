"""Settings saves preserve independently changed board preferences and files."""

from copy import deepcopy
import errno
import json
import multiprocessing
import os
from pathlib import Path
from typing import Any

import pytest

from core import file_lock as locking, settings_persistence as persistence


def _seed(directory: Path, settings: dict[str, Any]) -> Path:
    """Write a complete initial document independently of the save implementation."""
    (directory / "default_settings.json").write_text("{}", encoding="utf-8")
    path = directory / "settings.json"
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return path


def _output_patch(board: str, value: str) -> persistence.VariantPreferencePatch:
    """Express one user's explicit output choice in the public save contract."""
    return persistence.VariantPreferencePatch(
        {("boards", board, "output_variant"): value}
    )


def _contending_process(
    directory: str,
    initial: dict[str, Any],
    connection: Any,
    operation: str,
) -> None:
    """Run real OS locking in a separate interpreter with controlled contention."""
    try:
        if operation == "first":
            original_read = persistence._read_current_settings

            def paused_read(path: Path) -> dict[str, Any]:
                result = original_read(path)
                connection.send("read")
                if not connection.poll(10) or connection.recv() != "release":
                    raise RuntimeError("The parent did not release the first writer")
                return result

            persistence._read_current_settings = paused_read
        else:
            original_lock = locking._try_file_lock
            attempted = False

            def record_attempt(handle: Any) -> None:
                nonlocal attempted
                if not attempted:
                    connection.send("attempt")
                    attempted = True
                original_lock(handle)

            locking._try_file_lock = record_attempt
        if operation == "load":
            result = persistence.load_settings_document(directory)
        else:
            result = persistence.save_settings_document(
                directory,
                initial,
                _output_patch(operation, "A" if operation == "first" else "B"),
            )
        connection.send(("done", result))
    except BaseException as error:
        connection.send(("error", repr(error)))
    finally:
        connection.close()


@pytest.mark.parametrize("second_operation", ["second", "load"])
def test_processes_lock_read_merge_replace_and_startup_migration(
    tmp_path: Path, second_operation: str
) -> None:
    """A second writer or startup migration must wait for the first committed edit."""
    initial = {"custom": "keep"}
    path = _seed(tmp_path, initial)
    if second_operation == "load":
        (tmp_path / "default_settings.json").write_text(
            '{"general": {"new_preference": true}}', encoding="utf-8"
        )
    original_bytes = path.read_bytes()
    context = multiprocessing.get_context("spawn")
    first_parent, first_child = context.Pipe()
    second_parent, second_child = context.Pipe()
    first = context.Process(
        target=_contending_process,
        args=(str(tmp_path), initial, first_child, "first"),
    )
    second = context.Process(
        target=_contending_process,
        args=(str(tmp_path), initial, second_child, second_operation),
    )
    first.start()
    try:
        assert first_parent.poll(10)
        assert first_parent.recv() == "read"
        second.start()
        assert second_parent.poll(10)
        assert second_parent.recv() == "attempt"
        assert not second_parent.poll(0.1)
        assert path.read_bytes() == original_bytes
        first_parent.send("release")
        assert first_parent.poll(10)
        assert first_parent.recv()[0] == "done"
        assert second_parent.poll(10)
        status, result = second_parent.recv()
        assert status == "done", result
        assert result["variants"]["boards"]["first"]["output_variant"] == "A"
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved == result
        assert saved["custom"] == "keep"
        if second_operation == "load":
            assert saved["general"]["new_preference"] is True
        else:
            assert saved["variants"]["boards"]["second"]["output_variant"] == "B"
    finally:
        for process in (first, second):
            if process.pid is not None:
                process.join(timeout=10)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
        for connection in (first_parent, first_child, second_parent, second_child):
            connection.close()
    assert first.exitcode == second.exitcode == 0


def test_same_board_independent_fields_merge_and_same_field_last_writer_wins(
    tmp_path: Path,
) -> None:
    """Stale controls do not replay other settings when an independent field changes."""
    display = {
        "differences_only": False,
        "variant_order": ["", "A", "B"],
        "widths": {"value": 100, "lcsc": 100},
    }
    original = {
        "variants": {
            "future_metadata": {"keep": True},
            "boards": {"pcb": {"output_variant": "", "display_preferences": display}},
        }
    }
    _seed(tmp_path, original)
    first, second = deepcopy(original), deepcopy(original)
    prefix = ("boards", "pcb", "display_preferences")
    first_display = {
        **display,
        "differences_only": True,
        "widths": {"value": 150, "lcsc": 100},
    }
    second_display = {
        **display,
        "variant_order": ("", "B", "A"),
        "widths": {"value": 100, "lcsc": 180},
    }
    persistence.save_settings_document(
        tmp_path,
        first,
        persistence.changed_variant_preferences(display, first_display, prefix),
    )
    persistence.save_settings_document(tmp_path, first, _output_patch("pcb", "A"))
    persistence.save_settings_document(
        tmp_path,
        second,
        persistence.changed_variant_preferences(display, second_display, prefix),
    )
    persistence.save_settings_document(tmp_path, second, _output_patch("pcb", "B"))
    saved = persistence.save_settings_document(tmp_path, original)
    assert saved["variants"] == {
        "future_metadata": {"keep": True},
        "boards": {
            "pcb": {
                "output_variant": "B",
                "display_preferences": {
                    "differences_only": True,
                    "variant_order": ["", "B", "A"],
                    "widths": {"value": 150, "lcsc": 180},
                },
            }
        },
    }
    assert first == second == original


def test_display_deletions_preserve_concurrently_added_widths(tmp_path: Path) -> None:
    """Removing one saved width does not discard another window's new width."""
    before = {"widths": {"footprint": 200, "value": 100}}
    original = {"variants": {"boards": {"pcb": {"display_preferences": before}}}}
    _seed(tmp_path, original)
    prefix = ("boards", "pcb", "display_preferences")
    persistence.save_settings_document(
        tmp_path,
        original,
        persistence.VariantPreferencePatch({(*prefix, "widths", "lcsc"): 160}),
    )
    saved = persistence.save_settings_document(
        tmp_path,
        original,
        persistence.changed_variant_preferences(
            before, {"widths": {"value": 100}}, prefix
        ),
    )
    assert saved["variants"]["boards"]["pcb"]["display_preferences"] == {
        "widths": {"value": 100, "lcsc": 160}
    }


@pytest.mark.parametrize("damaged", ['{"variants":', "[]", "null"])
def test_damage_during_save_preserves_file_and_input(
    tmp_path: Path, damaged: str
) -> None:
    """A file damaged after opening is never overwritten by a stale settings save."""
    ordinary = {"custom": "keep"}
    path = _seed(tmp_path, ordinary)
    path.write_text(damaged, encoding="utf-8")
    with pytest.raises(persistence.SettingsPersistenceError, match="invalid"):
        persistence.save_settings_document(
            tmp_path, ordinary, _output_patch("pcb", "A")
        )
    assert path.read_text(encoding="utf-8") == damaged
    assert ordinary == {"custom": "keep"}
    assert not (tmp_path / "settings.json.damaged").exists()


@pytest.mark.parametrize("variants", [[], {"boards": []}, {"boards": {"pcb": []}}])
def test_variant_patch_preserves_malformed_structure(
    tmp_path: Path, variants: Any
) -> None:
    """An explicit update cannot replace an unreadable preference container."""
    path = _seed(tmp_path, {"variants": variants})
    original = path.read_bytes()
    with pytest.raises(persistence.SettingsPersistenceError, match="invalid"):
        persistence.save_settings_document(tmp_path, {}, _output_patch("pcb", "A"))
    assert path.read_bytes() == original
    # An ordinary save has no authority to interpret or replace opaque variants.
    result = persistence.save_settings_document(tmp_path, {"custom": "keep"})
    assert result == {"custom": "keep", "variants": variants}


def test_startup_recovery_retains_damaged_bytes_and_settles(tmp_path: Path) -> None:
    """Locked startup retains the established damaged-file recovery behavior."""
    path = _seed(tmp_path, {})
    (tmp_path / "default_settings.json").write_text('{"general": {"enabled": true}}')
    damaged = '{"variants":'
    path.write_text(damaged, encoding="utf-8")
    expected = {"general": {"enabled": True}}
    assert persistence.load_settings_document(tmp_path) == expected
    assert json.loads(path.read_text()) == expected
    assert (tmp_path / "settings.json.damaged").read_text() == damaged
    assert persistence.load_settings_document(tmp_path) == expected


@pytest.mark.skipif(os.name == "nt", reason="Requires POSIX directory permissions")
def test_settled_settings_open_without_writing_to_readonly_plugin_directory(
    tmp_path: Path,
) -> None:
    """Opening existing settings must not acquire new filesystem write requirements."""
    expected = {
        "custom": "keep",
        "variants": {"boards": {"pcb": {"output_variant": "A"}}},
    }
    path = _seed(tmp_path, expected)
    original = path.read_bytes()
    tmp_path.chmod(0o555)
    try:
        # The prior loader supports this installed-directory state without writes.
        assert persistence.resolve_settings(tmp_path) == (expected, False)
        assert persistence.load_settings_document(tmp_path) == expected
        assert path.read_bytes() == original
        assert sorted(entry.name for entry in tmp_path.iterdir()) == [
            "default_settings.json",
            "settings.json",
        ]
    finally:
        tmp_path.chmod(0o755)


@pytest.mark.parametrize("failure", ["write", "replace", "lock", "timeout"])
def test_failed_save_preserves_disk_and_caller_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """No unsuccessful write may publish a preference or abandon a temporary file."""
    initial = {"variants": {"boards": {"pcb": {"output_variant": "A"}}}}
    path = _seed(tmp_path, initial)
    original = path.read_bytes()

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(errno.EIO, "test save failure")

    def busy(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(errno.EACCES, "test lock busy")

    if failure == "write":
        monkeypatch.setattr(persistence.json, "dump", fail)
    elif failure == "replace":
        monkeypatch.setattr(persistence.os, "replace", fail)
    elif failure == "lock":
        monkeypatch.setattr(locking, "_try_file_lock", fail)
    else:
        monkeypatch.setattr(locking, "_try_file_lock", busy)
        monkeypatch.setattr(persistence, "_LOCK_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(OSError):
        persistence.save_settings_document(tmp_path, initial, _output_patch("pcb", "B"))
    assert initial["variants"]["boards"]["pcb"]["output_variant"] == "A"
    assert path.read_bytes() == original
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [
        "default_settings.json",
        "settings.json",
        "settings.json.lock",
    ]
