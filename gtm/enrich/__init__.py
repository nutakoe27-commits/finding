"""Досье: третье звено конвейера.

Наружу нужны две вещи: собрать досье по одному ожиданию и прогнать стадию
по всем прошедшим фильтр.
"""

from gtm.enrich.dossier import STAGE_NAME, build_dossier, run_enrich

__all__ = ["STAGE_NAME", "build_dossier", "run_enrich"]
