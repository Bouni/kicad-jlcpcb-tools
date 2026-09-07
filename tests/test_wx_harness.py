"""Verify import lifetimes and explicit wx behavior used by GUI regressions."""

import importlib
from pathlib import Path
import sys
from types import ModuleType
from typing import Optional

import pytest

from tests import wx_harness


def test_correction_runtime_flags_have_no_false_membership() -> None:
    """A confirmation style cannot accidentally include the error icon bit."""
    with wx_harness.load_correction_modules() as runtime:
        wx = runtime.wx
        style = wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING
        assert style & wx.ICON_ERROR == 0
        assert wx.NOT_FOUND == -1


def test_real_correction_siblings_share_model_events_and_gui() -> None:
    """Real storage and manager imports use one model class and event factory."""
    with wx_harness.load_correction_modules(names=("events",)) as runtime:
        assert runtime.library.Correction is runtime.data.Correction
        assert (
            runtime.corrections.PopulateFootprintListEvent
            is runtime.events.PopulateFootprintListEvent
        )
        assert runtime.library.wx is runtime.corrections.wx is runtime.wx
        assert (
            importlib.import_module(f"{runtime.package_name}.events") is runtime.events
        )
        package = runtime.package_name
    assert not any(
        name == package or name.startswith(package + ".") for name in sys.modules
    )


@pytest.fixture
def source_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide independent modules whose imports expose duplicate identities."""
    monkeypatch.setattr(wx_harness, "ROOT", tmp_path)
    (tmp_path / "first.py").write_text(
        "import wx\nclass Token: pass\n"
        "def deferred():\n    from . import second\n    return second\n"
    )
    (tmp_path / "second.py").write_text(
        "from .first import Token\nimport wx\nimport pcbnew\n"
    )
    (tmp_path / "broken.py").write_text(
        "from . import second\nraise RuntimeError('broken import')\n"
    )
    return tmp_path


def test_siblings_keep_class_wx_and_runtime_identity_until_scope_exit(
    source_tree: Path,
) -> None:
    """Deferred imports during the test see exactly the already loaded modules."""
    package = "_harness_identity"
    replacements = wx_harness.wx_stubs()
    pcbnew = wx_harness.module("pcbnew")
    replacements["pcbnew"] = pcbnew
    with wx_harness.load_siblings(package, ("first", "second"), replacements) as loaded:
        assert loaded["first"] is importlib.import_module(f"{package}.first")
        assert loaded["first"].deferred() is loaded["second"]
        assert loaded["second"].Token is loaded["first"].Token
        assert loaded["first"].wx is loaded["second"].wx is replacements["wx"]
        assert loaded["first"].wx.dataview is replacements["wx.dataview"]
        assert loaded["second"].pcbnew is pcbnew
    assert not any(
        name == package or name.startswith(package + ".") for name in sys.modules
    )


@pytest.mark.parametrize("failure", [None, "body", "import"])
def test_siblings_restore_prior_modules_and_remove_transitive_imports(
    source_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Optional[str],  # noqa: UP045
) -> None:
    """Both import errors and test-body failures restore the complete namespace."""
    package = "_harness_restoration"
    previous = {
        package: wx_harness.module(package),
        f"{package}.first": wx_harness.module(f"{package}.first"),
        f"{package}.preserved": wx_harness.module(f"{package}.preserved"),
        "wx": wx_harness.module("wx"),
        "pcbnew": wx_harness.module("pcbnew"),
    }
    for name, value in previous.items():
        monkeypatch.setitem(sys.modules, name, value)
    replacements = wx_harness.wx_stubs()
    replacements["pcbnew"] = wx_harness.module("pcbnew")
    names = ("first", "broken") if failure == "import" else ("first",)

    def exercise() -> None:
        with wx_harness.load_siblings(package, names, replacements) as loaded:
            loaded["first"].deferred()
            assert sys.modules[f"{package}.first"] is not previous[f"{package}.first"]
            if failure == "body":
                raise RuntimeError("broken body")

    if failure:
        with pytest.raises(RuntimeError, match=f"broken {failure}"):
            exercise()
    else:
        exercise()
    for name, value in previous.items():
        assert sys.modules[name] is value
    assert f"{package}.second" not in sys.modules
    assert f"{package}.broken" not in sys.modules


def test_siblings_respect_supplied_package_and_sibling_replacements(
    source_tree: Path,
) -> None:
    """A dependency intentionally replaced by a fixture is not imported again."""
    package = "_harness_replacement"
    stubs = wx_harness.package_stubs(package)
    stubs.update(wx_harness.wx_stubs())
    supplied = wx_harness.module(f"{package}.second", sentinel=object())
    stubs[f"{package}.second"] = supplied
    with wx_harness.load_siblings(package, ("first", "second"), stubs) as loaded:
        assert sys.modules[package] is stubs[package]
        assert loaded["second"] is supplied
        assert loaded["first"].deferred() is supplied


def test_existing_one_shot_loader_releases_stubs_before_return(
    source_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing callers keep their module reference without a persistent scope."""
    package = "_harness_one_shot"
    prior_wx = wx_harness.module("wx")
    monkeypatch.setitem(sys.modules, "wx", prior_wx)
    replacements = wx_harness.package_stubs(package)
    replacements.update(wx_harness.wx_stubs())
    loaded = wx_harness.load(package, "first", replacements)
    assert loaded.wx is replacements["wx"]
    assert sys.modules["wx"] is prior_wx
    assert package not in sys.modules
    assert f"{package}.first" not in sys.modules


@pytest.mark.parametrize("failure", [None, "body", "import"])
def test_reused_package_restores_child_bindings_and_reloads_next_scope(
    source_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Optional[str],  # noqa: UP045
) -> None:
    """Reusing a supplied parent cannot retain stale imported child objects."""
    package = "_harness_reused"
    replacements = wx_harness.package_stubs(package)
    replacements.update(wx_harness.wx_stubs())
    replacements["pcbnew"] = wx_harness.module("pcbnew")
    parent = replacements[package]
    original_first = wx_harness.module(f"{package}.first")
    original_second = wx_harness.module(f"{package}.second", Token=object())
    parent.first = original_first
    parent.second = original_second
    parent.unrelated = object()
    original_bindings = dict(vars(parent))
    monkeypatch.setitem(sys.modules, package, parent)
    monkeypatch.setitem(sys.modules, original_first.__name__, original_first)
    monkeypatch.setitem(sys.modules, original_second.__name__, original_second)
    names = ("first", "broken") if failure == "import" else ("first",)
    first_scope = []

    def exercise() -> None:
        with wx_harness.load_siblings(package, names, replacements) as loaded:
            first_scope.append(loaded["first"])
            assert loaded["first"].deferred().Token is loaded["first"].Token
            if failure == "body":
                raise RuntimeError("broken body")

    if failure:
        with pytest.raises(RuntimeError, match=f"broken {failure}"):
            exercise()
    else:
        exercise()
    assert vars(parent) == original_bindings
    assert sys.modules[original_first.__name__] is original_first
    assert sys.modules[original_second.__name__] is original_second
    assert not hasattr(parent, "broken")
    with wx_harness.load_siblings(package, ("first",), replacements) as loaded:
        assert loaded["first"].deferred().Token is loaded["first"].Token
        assert all(loaded["first"] is not earlier for earlier in first_scope)
    assert vars(parent) == original_bindings


def test_nested_distinct_packages_restore_outer_deferred_imports_and_wx(
    source_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inner fixture cannot replace the outer fixture's deferred dependencies."""
    unrelated = wx_harness.module("_harness_unrelated")
    monkeypatch.setitem(sys.modules, unrelated.__name__, unrelated)
    outer = wx_harness.wx_stubs()
    outer["pcbnew"] = wx_harness.module("pcbnew")
    inner = wx_harness.wx_stubs()
    inner["pcbnew"] = wx_harness.module("pcbnew")
    with wx_harness.load_siblings("_harness_outer", ("first",), outer) as loaded_outer:
        with wx_harness.load_siblings(
            "_harness_inner", ("first",), inner
        ) as loaded_inner:
            assert loaded_inner["first"].deferred().wx is inner["wx"]
            assert sys.modules[unrelated.__name__] is unrelated
        assert sys.modules["wx"] is outer["wx"]
        assert sys.modules["pcbnew"] is outer["pcbnew"]
        assert loaded_outer["first"].deferred().wx is outer["wx"]
        assert sys.modules[unrelated.__name__] is unrelated
    assert sys.modules[unrelated.__name__] is unrelated


@pytest.mark.parametrize("failure", [False, True])
def test_preloaded_wx_children_cannot_replace_scoped_event_factory(
    monkeypatch: pytest.MonkeyPatch,
    failure: bool,
) -> None:
    """Even an environment with wx already imported uses the intended event stub."""
    foreign_event = object()
    prior = {
        "wx.lib": wx_harness.module("wx.lib"),
        "wx.lib.newevent": wx_harness.module(
            "wx.lib.newevent", NewEvent=lambda: (foreign_event, object())
        ),
    }
    for name, value in prior.items():
        monkeypatch.setitem(sys.modules, name, value)

    def exercise() -> None:
        with wx_harness.load_correction_modules(names=("events",)) as runtime:
            assert runtime.events.MessageEvent is not foreign_event
            if failure:
                raise RuntimeError("broken body")

    if failure:
        with pytest.raises(RuntimeError, match="broken body"):
            exercise()
    else:
        exercise()
    for name, value in prior.items():
        assert sys.modules[name] is value


def test_explicit_wx_event_factory_replacement_is_preserved() -> None:
    """Namespace isolation preserves deliberate wx child replacements."""
    supplied_event = object()
    wx = wx_harness.wx_stubs(Dialog=type("Dialog", (), {}))
    wx["wx.lib"] = wx_harness.module("wx.lib", __path__=[])
    wx["wx.lib.newevent"] = wx_harness.module(
        "wx.lib.newevent", NewEvent=lambda: (supplied_event, object())
    )
    with wx_harness.load_correction_modules(wx=wx, names=("events",)) as runtime:
        assert runtime.events.MessageEvent is supplied_event
        assert runtime.wx.lib is wx["wx.lib"]
        assert runtime.wx.lib.newevent is wx["wx.lib.newevent"]


def test_one_shot_event_loader_also_isolates_preloaded_wx_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Events prepared for mainwindow stubs cannot use a foreign native factory."""
    foreign_event = object()
    prior = wx_harness.module(
        "wx.lib.newevent", NewEvent=lambda: (foreign_event, object())
    )
    monkeypatch.setitem(sys.modules, prior.__name__, prior)
    stubs = wx_harness.package_stubs("_harness_one_shot_events")
    stubs.update(wx_harness.wx_stubs())
    loaded = wx_harness.load("_harness_one_shot_events", "events", stubs)
    assert loaded.MessageEvent is not foreign_event
    assert sys.modules[prior.__name__] is prior


def test_fake_wx_flags_are_distinct_and_missing_callables_are_errors() -> None:
    """Strict stubs cannot silently accept an unimplemented native operation."""
    stubs = wx_harness.wx_stubs()
    wx = stubs["wx"]
    flags = [wx.OK, wx.CANCEL, wx.ICON_ERROR, wx.ICON_WARNING, wx.ICON_QUESTION]
    assert len(set(flags)) == len(flags)
    assert all(flag > 0 and flag & (flag - 1) == 0 for flag in flags)
    assert wx.NOT_FOUND == -1
    for name in ("MessageBox", "TextCtrl", "SetValue", "__unexpected__"):
        with pytest.raises(AttributeError, match=name):
            getattr(wx, name)
    assert isinstance(stubs["wx.dataview"], ModuleType)
