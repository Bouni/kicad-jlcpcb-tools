"""Regression tests for the settings dialog's static labels and LCSC dropdown.

``settings.py`` normally runs inside KiCad and imports wxPython at module load
time.  The shared harness loads it under a private synthetic package with a
fake ``wx`` whose controls record labels, values and selections, so the
dialog's real construction and change-handling code runs without a GUI.

Covers https://github.com/Bouni/kicad-jlcpcb-tools/issues/778: checkbox labels
must describe the behaviour when checked and never change with the state, and
the LCSC priority setting is a two-way choice rather than an on/off switch.
"""

import types
from typing import Any, Optional

import pytest

from .wx_harness import load, module, package_stubs, wx_stubs

_PACKAGE = "settings_dialog_tests"


class _FakeWidget:
    """Stand-in for the wx objects whose state these tests never inspect."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def __getattr__(self, name):
        """Answer wx-style method calls with a no-op, and nothing else."""
        if name[:1].isupper():
            return lambda *_args, **_kwargs: None
        raise AttributeError(name)


# Everything settings.py builds that these tests do not look at.  Naming them
# keeps the fake wx strict: a name that is not here and not a control below
# raises AttributeError rather than quietly becoming callable.
_INERT = (
    "AcceleratorEntry",
    "AcceleratorTable",
    "BoxSizer",
    "Button",
    "Colour",
    "DefaultPosition",
    "DefaultSize",
    "DirPickerCtrl",
    "FilePickerCtrl",
    "FlexGridSizer",
    "MemoryDC",
    "NewId",
    "NullBitmap",
    "Pen",
    "Size",
    "SpinCtrl",
    "StaticBoxSizer",
    "ToolTip",
)


class _FakeBitmap:
    """Bitmap stand-in that remembers which icon file it was loaded from."""

    def __init__(self, filename):
        self.filename = filename

    def ConvertToImage(self):
        return self

    def ConvertToBitmap(self):
        return _FakeBitmap(self.filename)

    def GetSize(self):
        return (24, 24)


class _Control:
    """Strict base for controls whose state the tests inspect."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.name = kwargs.get("name", "")
        self.tooltip = None
        self.handler = None

    def GetName(self):
        return self.name

    def SetToolTip(self, tooltip):
        self.tooltip = tooltip

    def Bind(self, _event_type, handler):
        self.handler = handler


class _CheckBox(_Control):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.label = kwargs.get("label", "")
        self.value = False
        self.enabled = True

    def SetLabel(self, label):
        self.label = label

    def GetLabel(self):
        return self.label

    def SetValue(self, value):
        self.value = bool(value)

    def GetValue(self):
        return self.value

    def Enable(self, enable=True):
        self.enabled = bool(enable)

    def Disable(self):
        self.enabled = False

    def IsEnabled(self):
        return self.enabled


class _ComboBox(_Control):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.choices = list(kwargs.get("choices", []))
        self.value = kwargs.get("value", "")

    def SetStringSelection(self, text):
        if text not in self.choices:
            return False
        self.value = text
        return True

    def GetStringSelection(self):
        return self.value

    def GetValue(self):
        return self.value


class _StaticText(_Control):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.label = kwargs.get("label", "")

    def GetLabel(self):
        return self.label


class _StaticBitmap(_Control):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bitmap = args[2] if len(args) > 2 else kwargs.get("bitmap")

    def SetBitmap(self, bitmap):
        self.bitmap = bitmap


class _Dialog(_FakeWidget):
    """wx.Dialog stand-in whose window methods are all no-ops."""


def _load_settings_module() -> types.ModuleType:
    """Load settings.py with deterministic stubs for its GUI dependencies."""
    library_config = types.SimpleNamespace(display_name="Full Library - All Parts")
    stubs = package_stubs(_PACKAGE, ("bom_estimation",))
    stubs.update(
        wx_stubs(
            submodules=(),
            Dialog=_Dialog,
            CheckBox=_CheckBox,
            ComboBox=_ComboBox,
            StaticText=_StaticText,
            StaticBitmap=_StaticBitmap,
            posted_events=[],
            **dict.fromkeys(_INERT, _FakeWidget),
        )
    )
    wx = stubs["wx"]
    wx.PostEvent = lambda target, event: wx.posted_events.append((target, event))
    stubs.update(
        {
            f"{_PACKAGE}.bom_estimation.help_text": module(
                f"{_PACKAGE}.bom_estimation.help_text",
                show_bom_estimator_help=lambda *_args, **_kwargs: None,
            ),
            f"{_PACKAGE}.dblib": module(
                f"{_PACKAGE}.dblib", LIBRARY_CONFIGS={"current-parts": library_config}
            ),
            f"{_PACKAGE}.helpers": module(
                f"{_PACKAGE}.helpers",
                HighResWxSize=lambda _window, size: size,
                loadBitmapScaled=lambda filename, *_a, **_k: _FakeBitmap(filename),
            ),
        }
    )
    stubs[f"{_PACKAGE}.events"] = load(_PACKAGE, "events", stubs)
    return load(_PACKAGE, "settings", stubs)


settings = _load_settings_module()
SettingsDialog = settings.SettingsDialog
_wx = settings.wx

# Checkbox attribute on the dialog -> (settings section, settings key).
_BOOLEAN_SETTINGS = {
    "tented_vias_setting": ("gerber", "tented_vias"),
    "fill_zones_setting": ("gerber", "fill_zones"),
    "force_drc_setting": ("gerber", "force_drc"),
    "plot_values_setting": ("gerber", "plot_values"),
    "plot_references_setting": ("gerber", "plot_references"),
    "subtract_mask_from_silk_setting": ("gerber", "subtract_mask_from_silk"),
    "lcsc_bom_cpl_setting": ("gerber", "lcsc_bom_cpl"),
    "order_number_setting": ("general", "order_number"),
    "highlight_matches_setting": ("highlighting", "matches"),
    "bom_estimator_show_setting": ("general", "bom_estimator_show"),
    "part_preferences_remember_lcsc_assignments_setting": (
        "part_preferences",
        "remember_lcsc_assignments",
    ),
    "part_preferences_fill_empty_lcsc_assignments_on_open_setting": (
        "part_preferences",
        "fill_empty_lcsc_assignments_on_open",
    ),
}

# Every label describes what happens when the box is checked.
_EXPECTED_LABELS = {
    "tented_vias_setting": "Tent vias",
    "fill_zones_setting": "Fill zones",
    "force_drc_setting": (
        "Force DRC check before Gerber export (saves board and fills zones)"
    ),
    "plot_values_setting": "Plot values on silkscreen",
    "plot_references_setting": "Plot references on silkscreen",
    "subtract_mask_from_silk_setting": "Subtract soldermask from silkscreen",
    "lcsc_bom_cpl_setting": "Add parts without LCSC number to BOM/CPL",
    "order_number_setting": "Check for an order/serial number placeholder on export",
    "highlight_matches_setting": "Highlight search matches",
    "bom_estimator_show_setting": "Show BOM cost estimator",
    "part_preferences_remember_lcsc_assignments_setting": "Remember my part preferences",
    "part_preferences_fill_empty_lcsc_assignments_on_open_setting": (
        "Parts preferences fill in empty LCSC assignments"
    ),
}


@pytest.fixture(autouse=True)
def _clear_posted_events():
    """Start every test with an empty record of posted UpdateSetting events."""
    del _wx.posted_events[:]


def _settings(flag: bool) -> dict[str, dict[str, Any]]:
    """Return a settings dict with every boolean checkbox setting set to ``flag``."""
    settings_dict = {
        "gerber": {},
        "general": {"lcsc_priority": True},
        "highlighting": {},
        "part_preferences": {},
        "library": {"selected_library": "current-parts", "data_path": ""},
        "hooks": {"pre_script": "", "post_script": "", "timeout_seconds": 30},
    }
    for section, key in _BOOLEAN_SETTINGS.values():
        settings_dict[section][key] = flag
    return settings_dict


def _dialog(settings_dict):
    """Construct the dialog against a fake main window holding ``settings_dict``."""
    parent = types.SimpleNamespace(
        window=object(),
        scale_factor=1.0,
        settings=settings_dict,
        library=types.SimpleNamespace(datadir="/data"),
    )
    return SettingsDialog(parent)


def _fire(control):
    """Invoke the change handler bound to ``control``; return the posted events."""
    before = len(_wx.posted_events)
    control.handler(types.SimpleNamespace(GetEventObject=lambda: control))
    return [event for _target, event in _wx.posted_events[before:]]


@pytest.mark.parametrize("attribute", sorted(_EXPECTED_LABELS))
def test_checkbox_label_is_static_and_describes_checked_behaviour(attribute):
    """A label must read the same whether the setting is on or off."""
    expected = _EXPECTED_LABELS[attribute]

    labels = {
        flag: getattr(_dialog(_settings(flag)), attribute).GetLabel()
        for flag in (True, False)
    }

    assert labels == {True: expected, False: expected}


@pytest.mark.parametrize(
    ("flag", "icon"), [(True, "tented.png"), (False, "untented.png")]
)
def test_tented_vias_icon_follows_state(flag, icon):
    """The icon keeps illustrating the effect of the current setting."""
    dialog = _dialog(_settings(flag))

    assert dialog.tented_vias_image.bitmap.filename == icon


@pytest.mark.parametrize(
    ("priority", "choice", "icon"),
    [(True, "Schematic", "schematic.png"), (False, "Database", "database-outline.png")],
)
def test_lcsc_priority_dropdown_reflects_setting(priority, choice, icon):
    """The stored boolean maps onto a Schematic/Database choice and icon."""
    settings_dict = _settings(True)
    settings_dict["general"]["lcsc_priority"] = priority

    dialog = _dialog(settings_dict)
    control = dialog.lcsc_priority_setting

    assert isinstance(control, _wx.ComboBox)
    assert (control.choices, control.GetStringSelection()) == (
        ["Schematic", "Database"],
        choice,
    )
    assert dialog.lcsc_priority_image.bitmap.filename == icon


@pytest.mark.parametrize(
    ("choice", "expected", "icon"),
    [("Database", False, "database-outline.png"), ("Schematic", True, "schematic.png")],
)
def test_selecting_lcsc_priority_posts_a_boolean(choice, expected, icon):
    """Choosing an entry persists the existing boolean setting, not the text."""
    dialog = _dialog(_settings(True))
    control = dialog.lcsc_priority_setting
    assert isinstance(control, _wx.ComboBox)

    control.SetStringSelection(choice)
    events = _fire(control)

    assert [(e.section, e.setting) for e in events] == [("general", "lcsc_priority")]
    assert events[0].value is expected
    assert dialog.lcsc_priority_image.bitmap.filename == icon


def test_force_drc_forces_and_disables_fill_zones():
    """Forcing DRC pins fill zones on; releasing it re-enables the checkbox."""
    settings_dict = _settings(True)
    settings_dict["gerber"]["fill_zones"] = False
    dialog = _dialog(settings_dict)
    fill_zones = dialog.fill_zones_setting

    assert (fill_zones.GetValue(), fill_zones.IsEnabled()) == (True, False)

    dialog.force_drc_setting.SetValue(False)
    _fire(dialog.force_drc_setting)

    assert fill_zones.IsEnabled()


def test_enabling_force_drc_also_persists_fill_zones():
    """Turning forced DRC on posts fill_zones=True before force_drc=True."""
    dialog = _dialog(_settings(False))

    dialog.force_drc_setting.SetValue(True)
    events = _fire(dialog.force_drc_setting)

    assert [(e.section, e.setting, e.value) for e in events] == [
        ("gerber", "fill_zones", True),
        ("gerber", "force_drc", True),
    ]


@pytest.mark.parametrize("attribute", _BOOLEAN_SETTINGS)
@pytest.mark.parametrize("enabled", [False, True])
def test_boolean_controls_load_and_dispatch_settings(
    attribute: str, enabled: bool
) -> None:
    """Each checkbox loads its saved state and dispatches its own setting."""
    settings_dict = _settings(not enabled)
    if attribute == "fill_zones_setting":
        settings_dict["gerber"]["force_drc"] = False
    dialog = _dialog(settings_dict)
    control = getattr(dialog, attribute)
    section, key = _BOOLEAN_SETTINGS[attribute]
    assert control.GetValue() is not enabled
    assert _wx.posted_events == []

    control.SetValue(enabled)
    events = _fire(control)

    assert control.GetValue() is enabled
    assert control.GetLabel() == _EXPECTED_LABELS[attribute]
    assert all(target is dialog.parent for target, _event in _wx.posted_events)
    expected = [(section, key, enabled)]
    if attribute == "force_drc_setting" and enabled:
        expected.insert(0, ("gerber", "fill_zones", True))
    assert [(event.section, event.setting, event.value) for event in events] == expected


@pytest.mark.parametrize(
    "existing", [None, {}], ids=["missing-section", "empty-section"]
)
def test_part_preferences_default_to_enabled_in_dialog(
    existing: Optional[dict[str, bool]],
) -> None:
    """Opening older settings enables missing options without emitting changes."""
    settings_dict = _settings(True)
    settings_dict.pop("part_preferences")
    if existing is not None:
        settings_dict["part_preferences"] = existing
    dialog = _dialog(settings_dict)

    assert dialog.part_preferences_remember_lcsc_assignments_setting.GetValue() is True
    assert (
        dialog.part_preferences_fill_empty_lcsc_assignments_on_open_setting.GetValue()
        is True
    )
    assert _wx.posted_events == []


def test_preference_settings_remain_editable_when_library_bootstrap_fails() -> None:
    """The real Settings constructor must work before a Library is available."""
    parent = types.SimpleNamespace(
        window=object(),
        scale_factor=1.0,
        settings=_settings(True),
        library=None,
    )
    dialog = SettingsDialog(parent)
    control = dialog.part_preferences_fill_empty_lcsc_assignments_on_open_setting
    assert control.GetValue() is True
    control.SetValue(False)
    events = _fire(control)
    assert len(events) == 1
    assert vars(events[0]) == {
        "section": "part_preferences",
        "setting": "fill_empty_lcsc_assignments_on_open",
        "value": False,
    }
