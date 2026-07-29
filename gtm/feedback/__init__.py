"""Обратная связь: то, ради чего система хранит историю.

У площадки цикл продажи короткий, а результат бинарный — забронировали
или нет. Значит, веса сигналов можно считать по фактам, а не гадать.

Наружу нужны четыре вещи: записать исход, принять таблицу исходов от
менеджера, показать воронку и предложить новые веса. Предложить —
именно так: конфиг калибровка не переписывает никогда.
"""

from gtm.feedback.calibrate import CalibrationReport, recalibrate
from gtm.feedback.outcomes import funnel, import_outcomes_csv, record_outcome

__all__ = [
    "CalibrationReport",
    "funnel",
    "import_outcomes_csv",
    "recalibrate",
    "record_outcome",
]
