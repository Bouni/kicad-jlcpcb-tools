"""Shared pytest setup for the common/ test suite.

Tests in this directory import modules from the repository root (e.g.
``bom_estimation``, ``enrichment``, ``common``). Adding the parent dir to
sys.path here lets every test file import them without repeating the
path-insert preamble that used to live at the top of each test module.
"""

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
