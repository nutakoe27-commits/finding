"""Учёт платных запросов и дневной лимит.

Каждый платный вызов логируется: что запросили, сколько стоило, какой
стадии принадлежит. Плюс жёсткий дневной лимит с остановкой — на случай
бесконечного цикла.

Порядок дорогих операций задаётся не здесь, а конвейером: дешёвые фильтры
рано, дорогие операции поздно. Этот модуль лишь не даёт превысить бюджет.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session

from gtm.observability import get_logger
from gtm.settings import get_settings
from gtm.storage import repo


class BudgetExceeded(RuntimeError):
    """Дневной лимит исчерпан. Стадия обязана остановиться, а не продолжать."""


class SpendGuard:
    """Проверка лимита перед вызовом и запись факта траты после.

    Использование:

        guard = SpendGuard(session, stage="enrich")
        guard.check(cost_rub=0.3)          # бросит BudgetExceeded
        ...вызов API...
        guard.record("dadata", "/findById/party", cost_rub=0.3, request_key=inn)
    """

    def __init__(self, session: Session, stage: str | None = None) -> None:
        self.session = session
        self.stage = stage
        self.log = get_logger("gtm.spend")
        settings = get_settings()
        self.budget_rub = settings.daily_api_budget_rub
        self.call_limit = settings.daily_api_call_limit

    def remaining(self) -> tuple[float, int]:
        spent, calls = repo.spend_today(self.session)
        return self.budget_rub - spent, self.call_limit - calls

    def check(self, cost_rub: float = 0.0) -> None:
        money_left, calls_left = self.remaining()
        if calls_left <= 0:
            raise BudgetExceeded(
                f"Дневной лимит вызовов исчерпан ({self.call_limit}). "
                f"Стадия '{self.stage}' остановлена."
            )
        if cost_rub > money_left:
            raise BudgetExceeded(
                f"Дневной бюджет исчерпан: осталось {money_left:.2f} ₽, "
                f"запрос стоит {cost_rub:.2f} ₽. Стадия '{self.stage}' остановлена."
            )

    def record(
        self,
        provider: str,
        endpoint: str | None = None,
        *,
        cost_rub: float = 0.0,
        units: float = 1.0,
        request_key: str | None = None,
        ok: bool = True,
    ) -> None:
        repo.log_api_spend(
            self.session,
            provider=provider,
            endpoint=endpoint,
            stage=self.stage,
            units=units,
            cost_rub=cost_rub,
            request_key=request_key,
            ok=ok,
        )

    @contextmanager
    def call(
        self,
        provider: str,
        endpoint: str | None = None,
        *,
        cost_rub: float = 0.0,
        request_key: str | None = None,
    ) -> Iterator[None]:
        """Проверить лимит, выполнить вызов, записать трату в любом случае.

        Неудачный запрос тоже стоит денег у большинства провайдеров —
        поэтому он тоже попадает в журнал, с ok=False.
        """
        self.check(cost_rub)
        ok = True
        try:
            yield
        except Exception:
            ok = False
            raise
        finally:
            self.record(
                provider, endpoint, cost_rub=cost_rub, request_key=request_key, ok=ok
            )


def daily_report(session: Session) -> str:
    spent, calls = repo.spend_today(session)
    settings = get_settings()
    return (
        f"Потрачено сегодня: {spent:.2f} ₽ из {settings.daily_api_budget_rub:.2f} ₽, "
        f"вызовов {calls} из {settings.daily_api_call_limit}"
    )
