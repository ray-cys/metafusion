"""Opt-in Formula 1 metadata and artwork extension for Kometa mode."""

from extensions.formula1.config import formula1_requested
from extensions.formula1.runner import partition_formula1_sections, run_formula1_extension

__all__ = (
    "formula1_requested",
    "partition_formula1_sections",
    "run_formula1_extension",
)
