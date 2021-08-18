THT = 0
SMD = 1
EXCLUDE_FROM_POS = 2
EXCLUDE_FROM_BOM = 3
NOT_IN_SCHEMATIC = 4

def get_bit(value, bit):
    return value & (1 << bit)


def set_bit(value, bit):
    return value | (1 << bit)


def clear_bit(value, bit):
    return value & ~(1 << bit)


def toggle_bit(value, bit):
    return value ^ (1 << bit)


def get_tht(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    return bool(get_bit(val, THT))


def get_smd(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    return bool(get_bit(val, SMD))


def get_exclude_from_pos(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    return bool(get_bit(val, EXCLUDE_FROM_POS))


def get_exclude_from_bom(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    return bool(get_bit(val, EXCLUDE_FROM_BOM))


def get_not_in_schematic(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    return bool(get_bit(val, NOT_IN_SCHEMATIC))


def set_tht(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = set_bit(val, THT)
    footprint.SetAttributes(val)
    return bool(val)


def set_smd(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = set_bit(val, SMD)
    footprint.SetAttributes(val)
    return bool(val)


def set_exclude_from_pos(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = set_bit(val, EXCLUDE_FROM_POS)
    footprint.SetAttributes(val)
    return bool(val)


def set_exclude_from_bom(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = set_bit(val, EXCLUDE_FROM_BOM)
    footprint.SetAttributes(val)
    return bool(val)


def set_not_in_schematic(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = set_bit(val, NOT_IN_SCHEMATIC)
    footprint.SetAttributes(val)
    return bool(val)


def toggle_tht(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = toggle_bit(val, THT)
    footprint.SetAttributes(val)
    return bool(val)


def toggle_smd(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = toggle_bit(val, SMD)
    footprint.SetAttributes(val)
    return bool(val)


def toggle_exclude_from_pos(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = toggle_bit(val, EXCLUDE_FROM_POS)
    footprint.SetAttributes(val)
    return bool(val)


def toggle_exclude_from_bom(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = toggle_bit(val, EXCLUDE_FROM_BOM)
    footprint.SetAttributes(val)
    return bool(val)


def toggle_not_in_schematic(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = toggle_bit(val, NOT_IN_SCHEMATIC)
    footprint.SetAttributes(val)
    return bool(val)
