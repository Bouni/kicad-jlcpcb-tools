"""Tests for legacy BOM settings migration."""

from bom_estimation.settings_migration import (  # pylint: disable=import-error
    LEGACY_BOM_SETTING_KEYS,
    drop_legacy_bom_settings,
)


def test_drop_legacy_bom_settings_removes_all_three_legacy_keys():
    """The three known legacy bom_* keys are removed from a settings dict."""
    general_settings = {
        "bom_order_handling_fee": 1.50,
        "bom_panelization_per_board_fee": 0.20,
        "bom_panelization_threshold_boards": 10,
        "bom_estimator_boards": 5,  # current key, must be preserved
        "highlight_standard_parts": True,
    }

    removed = drop_legacy_bom_settings(general_settings)

    assert removed is True
    assert "bom_order_handling_fee" not in general_settings
    assert "bom_panelization_per_board_fee" not in general_settings
    assert "bom_panelization_threshold_boards" not in general_settings
    # Current keys are untouched.
    assert general_settings["bom_estimator_boards"] == 5
    assert general_settings["highlight_standard_parts"] is True


def test_drop_legacy_bom_settings_returns_false_when_nothing_to_remove():
    """No keys to drop → returns False so caller can skip a save."""
    general_settings = {"bom_estimator_boards": 5}

    removed = drop_legacy_bom_settings(general_settings)

    assert removed is False
    assert general_settings == {"bom_estimator_boards": 5}


def test_legacy_keys_constant_lists_exactly_the_three_known_keys():
    """Pin the legacy-keys contract so removals are caught in review."""
    assert set(LEGACY_BOM_SETTING_KEYS) == {
        "bom_order_handling_fee",
        "bom_panelization_per_board_fee",
        "bom_panelization_threshold_boards",
    }
