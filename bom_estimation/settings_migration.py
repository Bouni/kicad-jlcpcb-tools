"""Migration helpers for BOM-estimator persisted settings.

Kept separate from mainwindow.py so the migration can be unit-tested without
loading the wx-bound dialog class.
"""

# Keys that the old standalone bom_estimator module persisted but the current
# pricing path no longer reads. Removed on load so they don't accumulate in
# users' settings.json forever.
LEGACY_BOM_SETTING_KEYS = (
    "bom_order_handling_fee",
    "bom_panelization_per_board_fee",
    "bom_panelization_threshold_boards",
)


def drop_legacy_bom_settings(general_settings: dict) -> bool:
    """Remove legacy bom_* keys from a general-settings dict.

    Returns True if any key was removed (caller should persist the change).
    """
    removed = False
    for legacy_key in LEGACY_BOM_SETTING_KEYS:
        if legacy_key in general_settings:
            general_settings.pop(legacy_key, None)
            removed = True
    return removed
