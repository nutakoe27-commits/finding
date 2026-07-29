"""Сведение записей источников к канонической компании по ИНН."""

from gtm.resolve.inn import extract_inn, is_valid_inn
from gtm.resolve.names import name_candidates, normalize_name
from gtm.resolve.resolver import resolve_company, resolve_quarantined, resolve_stats

__all__ = [
    "extract_inn",
    "is_valid_inn",
    "name_candidates",
    "normalize_name",
    "resolve_company",
    "resolve_quarantined",
    "resolve_stats",
]
