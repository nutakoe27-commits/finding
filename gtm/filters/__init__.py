"""Фильтрация по ICP — второе звено конвейера.

Здесь воронка сужается бесплатно: из трёхсот собранных фактов до обогащения
доходят десятки. Всё, что нужно для решения, уже лежит в базе — модуль не
делает ни одного внешнего запроса и ничего не знает о коллекторах.
"""

from gtm.filters.icp import Verdict, check_icp, estimate_attendees, run_filter
from gtm.filters.suppression import check_suppression, import_suppression_csv, suppress_on_bounce

__all__ = [
    "Verdict",
    "check_icp",
    "check_suppression",
    "estimate_attendees",
    "import_suppression_csv",
    "run_filter",
    "suppress_on_bounce",
]
