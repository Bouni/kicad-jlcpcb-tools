"""Generate part-selector keywords from schematic component values."""

import re


_CATEGORY_BY_REFERENCE_PREFIX = {
    'R': 'Resistors',
    'C': 'Capacitors',
    'D': 'Diodes',
    'Q': 'Transistors',
    'U': 'Integrated Circuits (ICs)',
}


def _normalize_resistance(value: str) -> str:
    """Convert common schematic resistance notation to the ohm symbol."""
    value = re.sub(r'(?i)^(\d+(?:\.\d+)?)r$', r'\1Ω', value)
    return re.sub(r'(?i)^(\d+)r(\d+)$', r'\1.\2Ω', value)


def build_search_keywords(reference: str, value: str, footprint: str) -> tuple[str, str, str]:
    """Return normalized keywords, category, and package suggestions.

    Value segments separated by ``/`` or ``_`` remain searchable individually,
    so values such as ``100nF/16V`` retain both capacitance and voltage ratings.
    A compact package-size suffix is returned separately for the selector's
    package filter instead of being added to the free-text keyword search.
    """
    prefix = re.sub(r'\d+$', '', reference.strip()).upper()
    category = _CATEGORY_BY_REFERENCE_PREFIX.get(prefix, '')
    values = [segment.strip() for segment in re.split(r'[/_]+', value) if segment.strip()]
    if prefix == 'R':
        values = [_normalize_resistance(segment) for segment in values]

    package_sizes = re.findall(r'(?<!\d)(\d{4})(?!\d)', footprint)
    package = package_sizes[0] if package_sizes else ''

    return ' '.join(values), category, package
