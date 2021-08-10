def get_tht(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    return bool(val & 0b1)


def get_smd(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    return bool(val & 0b10)


def get_exclude_from_pos(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    return bool(val & 0b100)


def get_exclude_from_bom(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    return bool(val & 0b1000)


def get_not_in_schematic(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    return bool(val & 0b10000)


def set_tht(footprint):
    """Not implemented"""
    pass


def set_smd(footprint):
    """Not implemented"""
    pass


def set_exclude_from_pos(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    footprint.SetAttributes(val ^ 0b100)


def set_exclude_from_bom(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    footprint.SetAttributes(val ^ 0b1000)


def set_not_in_schematic(footprint):
    """Not implemented"""
    pass
