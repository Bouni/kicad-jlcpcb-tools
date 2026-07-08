"""Helpers for reading and mutating KiCad footprint and board state.

KiCad 10 introduced design variants: footprint fields, DNP and the
exclude-from-BOM/POS flags can be overridden per variant, and the plain
getters (``GetValue``, ``IsDNP``, ``GetAttributes``, field text) always
return the base (default variant) state. The helpers in this module
resolve state through the ``*ForVariant`` APIs so the plugin reflects the
variant that is active in pcbnew, while falling back to the base state on
KiCad versions without variant support.
"""

import re

LCSC_NAME_REGEX = re.compile(r"lcsc|jlc", re.IGNORECASE)
LCSC_VALUE_REGEX = re.compile(r"^C\d+$")

EXCLUDE_FROM_POS = 2
EXCLUDE_FROM_BOM = 3


def get_current_variant(board) -> str:
    """Get the active design variant name of a board, "" for the default variant."""
    if not board:
        return ""
    getter = getattr(board, "GetCurrentVariant", None)
    if not callable(getter):
        return ""
    return str(getter() or "")


def get_footprint_variant(footprint) -> str:
    """Get the active design variant of the board the footprint belongs to."""
    get_board = getattr(footprint, "GetBoard", None)
    if not callable(get_board):
        return ""
    return get_current_variant(get_board())


def resolve_field_value(footprint, field_name: str, variant: str):
    """Resolve a field's text for a variant, or None when unsupported.

    KiCad falls back to the base field text when the variant does not
    override the field, so the result is always the effective text.
    """
    if not variant:
        return None
    resolver = getattr(footprint, "GetFieldValueForVariant", None)
    if not callable(resolver):
        return None
    return str(resolver(variant, field_name))


def get_variant_field_overrides(footprint, variant: str):
    """Get the {name: value} field overrides a footprint defines for a variant."""
    get_variant = getattr(footprint, "GetVariant", None)
    if not variant or not callable(get_variant):
        return {}
    fp_variant = get_variant(variant)
    if fp_variant is None:
        return {}
    return {str(name): str(value) for name, value in fp_variant.GetFields().items()}


def get_resolved_value(footprint) -> str:
    """Get the footprint's Value field resolved for the active design variant."""
    resolved = resolve_field_value(footprint, "Value", get_footprint_variant(footprint))
    if resolved is None:
        return footprint.GetValue()
    return resolved


def get_lcsc_value(fp):
    """Get the first lcsc number (C123456 for example) from the properties of the footprint.

    Field values are resolved for the active design variant, including
    fields that only exist as a variant override.
    """
    variant = get_footprint_variant(fp)
    try:
        for field in fp.GetFields():
            name = field.GetName()
            if not LCSC_NAME_REGEX.match(name):
                continue
            text = resolve_field_value(fp, name, variant)
            if text is None:
                text = field.GetText()
            if LCSC_VALUE_REGEX.match(text):
                return text
    except AttributeError:
        for key, value in fp.GetProperties().items():
            if LCSC_NAME_REGEX.match(key) and LCSC_VALUE_REGEX.match(value):
                return value
        return ""
    # The variant may override a field that has no base counterpart.
    for name, value in get_variant_field_overrides(fp, variant).items():
        if LCSC_NAME_REGEX.match(name) and LCSC_VALUE_REGEX.match(value):
            return value
    return ""


def set_lcsc_value(fp, lcsc: str):
    """Set an lcsc number on the footprint, using LCSC as property name if needed.

    When the active design variant explicitly overrides an lcsc-named
    field — even with an empty or placeholder text — that override is
    updated so the assignment is what the active variant shows; otherwise
    the base field is written and inherited by all variants without an
    override.
    """
    variant = get_footprint_variant(fp)
    lcsc_field = None
    for field in fp.GetFields():
        name = field.GetName()
        if not LCSC_NAME_REGEX.match(name):
            continue
        text = resolve_field_value(fp, name, variant)
        if text is None:
            text = field.GetText()
        if LCSC_VALUE_REGEX.match(text):
            lcsc_field = field

    if lcsc_field:
        field_name = lcsc_field.GetName()
        if _set_variant_field_override(fp, variant, field_name, lcsc):
            return
        fp.SetField(field_name, lcsc)
        return

    # Nothing resolves to a part number right now, but an lcsc-named
    # override (e.g. explicitly cleared for this variant) still owns the
    # value shown in the active variant and must be updated in place lest
    # the base write stays shadowed.
    override_name = None
    for name, value in get_variant_field_overrides(fp, variant).items():
        if not LCSC_NAME_REGEX.match(name):
            continue
        if LCSC_VALUE_REGEX.match(value):
            override_name = name
            break
        if override_name is None:
            override_name = name
    if override_name is not None:
        fp.GetVariant(variant).SetFieldValue(override_name, lcsc)
        return

    fp.SetField("LCSC", lcsc)
    if hasattr(fp, "GetFieldByName"):
        fp.GetFieldByName("LCSC").SetVisible(False)
    else:
        for field in fp.GetFields():
            if field.GetName() == "LCSC":
                field.SetVisible(False)
                break


def lcsc_override_cleared(fp) -> bool:
    """Return True when the active variant shadows the LCSC field with a non-part text.

    Used to distinguish "this variant explicitly has no part assigned"
    from "the footprint has no LCSC field at all" when syncing the board
    into the project database.
    """
    variant = get_footprint_variant(fp)
    if not variant:
        return False
    for name, value in get_variant_field_overrides(fp, variant).items():
        if LCSC_NAME_REGEX.match(name) and not LCSC_VALUE_REGEX.match(value):
            return True
    return False


def _set_variant_field_override(fp, variant: str, field_name: str, value: str) -> bool:
    """Update an existing variant field override, return True when done."""
    get_variant = getattr(fp, "GetVariant", None)
    if not variant or not callable(get_variant):
        return False
    fp_variant = get_variant(variant)
    if fp_variant is None or not fp_variant.HasFieldValue(field_name):
        return False
    fp_variant.SetFieldValue(field_name, value)
    return True


def _get_or_create_variant_override(footprint, variant: str):
    """Get the footprint's override object for a variant, creating it if needed.

    KiCad initializes a new footprint variant with a snapshot of the
    current base flags, so creating one to change a single flag preserves
    the effective state of the others.
    """
    get_variant = getattr(footprint, "GetVariant", None)
    add_variant = getattr(footprint, "AddVariant", None)
    if not callable(get_variant) or not callable(add_variant):
        return None
    existing = get_variant(variant)
    if existing is not None:
        return existing
    return add_variant(variant)


def _drop_variant_override_if_redundant(footprint, variant: str):
    """Delete a variant override that mirrors the base state exactly.

    Flag overrides are snapshots that stop inheriting later base changes,
    so a toggle round-trip would otherwise leave a behavior-changing
    override in the saved board file.
    """
    get_variant = getattr(footprint, "GetVariant", None)
    delete_variant = getattr(footprint, "DeleteVariant", None)
    if not callable(get_variant) or not callable(delete_variant):
        return
    override = get_variant(variant)
    if override is None or get_variant_field_overrides(footprint, variant):
        return
    is_dnp = getattr(footprint, "IsDNP", None)
    if override.GetDNP() != bool(is_dnp() if callable(is_dnp) else False):
        return
    attributes = footprint.GetAttributes()
    if override.GetExcludedFromBOM() != bool(get_bit(attributes, EXCLUDE_FROM_BOM)):
        return
    if override.GetExcludedFromPosFiles() != bool(
        get_bit(attributes, EXCLUDE_FROM_POS)
    ):
        return
    delete_variant(variant)


def get_valid_footprints(board):
    """Get all footprints that have a valid reference."""
    footprints = []
    for fp in board.GetFootprints():
        if re.match(r"[\w\d-]+", fp.GetReference()):
            footprints.append(fp)
    return footprints


def get_bit(value, bit):
    """Get the nth bit of a byte."""
    return value & (1 << bit)


def toggle_bit(value, bit):
    """Toggle the nth bit of a byte."""
    return value ^ (1 << bit)


def get_exclude_from_pos(footprint):
    """Get the 'exclude from POS' property of a footprint for the active variant."""
    if not footprint:
        return None
    variant = get_footprint_variant(footprint)
    getter = getattr(footprint, "GetExcludedFromPosFilesForVariant", None)
    if variant and callable(getter):
        return bool(getter(variant))
    val = footprint.GetAttributes()
    return bool(get_bit(val, EXCLUDE_FROM_POS))


def get_exclude_from_bom(footprint):
    """Get the 'exclude from BOM' property of a footprint for the active variant."""
    if not footprint:
        return None
    variant = get_footprint_variant(footprint)
    getter = getattr(footprint, "GetExcludedFromBOMForVariant", None)
    if variant and callable(getter):
        return bool(getter(variant))
    val = footprint.GetAttributes()
    return bool(get_bit(val, EXCLUDE_FROM_BOM))


def get_is_dnp(footprint):
    """Get the 'Do not place' state of a footprint for the active variant."""
    if not footprint:
        return False
    variant = get_footprint_variant(footprint)
    getter = getattr(footprint, "GetDNPForVariant", None)
    if variant and callable(getter):
        return bool(getter(variant))
    is_dnp = getattr(footprint, "IsDNP", None)
    if not callable(is_dnp):
        return False
    return bool(is_dnp())


def toggle_exclude_from_pos(footprint):
    """Toggle the 'exclude from POS' property of a footprint.

    With a non-default variant active the flag is flipped on the
    footprint's override for that variant, leaving the base state and
    other variants untouched.
    """
    if not footprint:
        return None
    variant = get_footprint_variant(footprint)
    if variant:
        override = _get_or_create_variant_override(footprint, variant)
        if override is not None:
            new_state = not get_exclude_from_pos(footprint)
            override.SetExcludedFromPosFiles(new_state)
            _drop_variant_override_if_redundant(footprint, variant)
            return new_state
    val = footprint.GetAttributes()
    val = toggle_bit(val, EXCLUDE_FROM_POS)
    footprint.SetAttributes(val)
    return bool(get_bit(val, EXCLUDE_FROM_POS))


def toggle_exclude_from_bom(footprint):
    """Toggle the 'exclude from BOM' property of a footprint.

    With a non-default variant active the flag is flipped on the
    footprint's override for that variant, leaving the base state and
    other variants untouched.
    """
    if not footprint:
        return None
    variant = get_footprint_variant(footprint)
    if variant:
        override = _get_or_create_variant_override(footprint, variant)
        if override is not None:
            new_state = not get_exclude_from_bom(footprint)
            override.SetExcludedFromBOM(new_state)
            _drop_variant_override_if_redundant(footprint, variant)
            return new_state
    val = footprint.GetAttributes()
    val = toggle_bit(val, EXCLUDE_FROM_BOM)
    footprint.SetAttributes(val)
    return bool(get_bit(val, EXCLUDE_FROM_BOM))
