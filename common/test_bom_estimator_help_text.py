"""Tests for shared BOM estimator help text."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from bom_estimation.help_text import (  # pylint: disable=import-error
    BOM_ESTIMATOR_HELP_TITLE,
    get_bom_estimator_help_text,
)


def test_bom_estimator_help_text_contains_core_disclaimers():
    """Help text includes network, rough estimate, and final-pricing disclaimers."""
    text = get_bom_estimator_help_text().lower()
    assert "network" in text
    assert "rough" in text
    assert "final pricing" in text
    assert "jlc" in text
    assert "issue" in text


def test_bom_estimator_help_title_is_stable():
    """Help title remains user-facing and stable."""
    assert BOM_ESTIMATOR_HELP_TITLE == "BOM estimator help"
