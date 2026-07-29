"""Ядро системы: разнородные факты сводятся к одной таблице ожиданий.

Наружу нужны две вещи: построить ожидания из накопленных фактов и
пересчитать приоритеты после правки весов в config/signals.yaml.
"""

from gtm.expectations.builder import (
    build_company_jubilees,
    build_event_anniversaries,
    build_from_facts,
    build_from_tenders,
    build_from_vacancies,
    contact_window,
)
from gtm.expectations.scoring import rescore_all, score_expectation

__all__ = [
    "build_company_jubilees",
    "build_event_anniversaries",
    "build_from_facts",
    "build_from_tenders",
    "build_from_vacancies",
    "contact_window",
    "rescore_all",
    "score_expectation",
]
