"""Утренний список — пятое звено конвейера и единственное, что видит менеджер.

Наружу нужны четыре вещи: собрать карточки, отрисовать их в двух форматах
и прогнать стадию с записью файлов.
"""

from gtm.deliver.daily import (
    STAGE_NAME,
    DeliveryItem,
    build_daily,
    render_csv,
    render_markdown,
    run_deliver,
)

__all__ = [
    "STAGE_NAME",
    "DeliveryItem",
    "build_daily",
    "render_csv",
    "render_markdown",
    "run_deliver",
]
