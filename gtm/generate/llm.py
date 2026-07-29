"""Клиент модели для генерации писем.

Клиентов два, и причина ровно одна: система обязана подниматься и
проверяться целиком без ключа. AnthropicClient пишет письмо живой моделью
и платит за каждый вызов; TemplateClient собирает то же содержимое из
досье по шаблону — без сети, детерминированно, бесплатно. Без второго
невозможны ни тесты, ни dry-run, ни показ клиенту.

Оба отдают одно и то же — JSON-строку с полями письма. Разбирает её
gtm.generate.letter.parse_letter; сам клиент про формат письма не знает.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol

from gtm.observability import get_logger
from gtm.settings import Settings, get_settings
from gtm.spend import SpendGuard

PROVIDER = "anthropic"
STAGE_NAME = "generate"

# Цена claude-opus-5 на июль 2026: $5 за миллион входных токенов и $25
# за миллион выходных. Курс держим рядом — журнал трат ведётся в рублях,
# и точность до копейки в нём не нужна, нужен порядок величины.
# ОБЕ КОНСТАНТЫ НАДО ПОДДЕРЖИВАТЬ В АКТУАЛЬНОМ СОСТОЯНИИ: при смене модели
# или курса цифры в api_spend поедут молча, и дневной лимит станет враньём.
_USD_PER_MTOKEN_INPUT = 5.0
_USD_PER_MTOKEN_OUTPUT = 25.0
_RUB_PER_USD = 90.0

# Технические константы.
# Кириллица токенизируется примерно по два символа на токен. Оценка грубая
# и нужна только для проверки лимита ДО вызова: сколько стоил запрос на
# самом деле, известно из usage в ответе.
_CHARS_PER_TOKEN = 2.0
_ENDPOINT = "/v1/messages"

# Уверенность шаблонного письма. Живая модель ставит её сама; здесь она
# считается по полноте досье, потому что больше опереться не на что.
_CONFIDENCE_FULL = 0.9  # есть дата прошлого мероприятия и его масштаб
_CONFIDENCE_DATED = 0.6  # есть дата, масштаб неизвестен
_CONFIDENCE_REASON = 0.4  # повод словами, без даты: юбилей, вакансия
_CONFIDENCE_BLIND = 0.15  # повода нет вовсе — такому письму опереться не на что

# Сколько фактов о площадке кладём в письмо. Правило из
# config/prompts/letter.md: максимум два, и оба релевантны масштабу.
_MAX_VENUE_FACTS = 2
_MAX_SUBJECT_CHARS = 60


class LLMError(RuntimeError):
    """Обращение к модели не состоялось: нет клиента, отказ, обрезанный ответ."""


class LLMClient(Protocol):
    """Что нужно генератору от клиента модели.

    `prompt` — то, что этот клиент умеет читать: живой модели уходит текст
    промпта, шаблонному — JSON из TemplateClient.payload. Разные входы —
    осознанно: разбирать собственный промпт обратно в факты значило бы
    ломаться при каждой правке config/prompts/letter.md.
    """

    name: str

    def complete(self, prompt: str) -> str: ...


# ------------------------------------------------------------------ деньги


def estimate_cost_rub(input_tokens: int, output_tokens: int) -> float:
    """Стоимость вызова в рублях по числу токенов."""
    usd = (
        input_tokens * _USD_PER_MTOKEN_INPUT + output_tokens * _USD_PER_MTOKEN_OUTPUT
    ) / 1_000_000
    return usd * _RUB_PER_USD


# ------------------------------------------------------------ живая модель


class AnthropicClient:
    """Письмо пишет модель. Каждый вызов платный.

    `guard` подключается снаружи: клиент строится без сессии (get_client),
    а трата обязана попасть в ту же сессию, что и стадия.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        max_tokens: int,
        client: Any | None = None,
        guard: SpendGuard | None = None,
    ) -> None:
        if not api_key:
            # Клиент без ключа бесполезен, и упасть он должен здесь,
            # а не первым вызовом посреди стадии.
            raise LLMError("generate: не задан GTM_ANTHROPIC_API_KEY")
        self.model = model
        self.max_tokens = max_tokens
        self.guard = guard
        # Модель в имени не для красоты: по generated_by в outreach потом
        # видно, чем именно написано письмо, которое сработало.
        self.name = f"{PROVIDER}/{model}"
        self.log = get_logger("gtm.generate.llm")
        self._sdk = client if client is not None else _build_sdk(api_key)

    def complete(self, prompt: str) -> str:
        guard = self.guard
        if guard is not None:
            # Проверяем по верхней оценке: реальная цена известна только из
            # ответа, а лимит должен сработать до того, как деньги потрачены.
            guard.check(estimate_cost_rub(int(len(prompt) / _CHARS_PER_TOKEN), self.max_tokens))
        try:
            message = self._sdk.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                # Письмо короткое и собирается из готовых фактов — думать тут
                # особо не над чем, а max_tokens делится между размышлением
                # и ответом, и на глубоком размышлении письму места не хватит.
                thinking={"type": "adaptive"},
                output_config={"effort": "low"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            if guard is not None:
                # Неудачный вызов у Anthropic не тарифицируется, но в журнале
                # он нужен: иначе непонятно, почему писем меньше, чем досье.
                guard.record(PROVIDER, _ENDPOINT, cost_rub=0.0, ok=False)
            raise LLMError(f"anthropic: запрос не выполнен ({exc})") from exc

        input_tokens, output_tokens = _usage(message)
        if guard is not None:
            guard.record(
                PROVIDER,
                _ENDPOINT,
                cost_rub=estimate_cost_rub(input_tokens, output_tokens),
                units=input_tokens + output_tokens,
            )

        stop_reason = getattr(message, "stop_reason", None)
        if stop_reason == "refusal":
            raise LLMError("anthropic: модель отказалась отвечать на этот промпт")
        if stop_reason == "max_tokens":
            # Молча отдать обрезанный JSON — худший вариант: письмо развалится
            # на разборе, а причина будет неочевидна.
            raise LLMError(
                f"anthropic: ответ не поместился в {self.max_tokens} токенов, "
                "поднимите GTM_LLM_MAX_TOKENS"
            )
        return _message_text(message)


def _build_sdk(api_key: str) -> Any:
    """SDK импортируется лениво: он в необязательной группе зависимостей,
    и без ключа система обходится вообще без него."""
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover — зависит от окружения
        raise LLMError(
            "generate: ключ задан, но пакет anthropic не установлен "
            "(pip install 'gtm[llm]')"
        ) from exc
    return anthropic.Anthropic(api_key=api_key)


def _usage(message: Any) -> tuple[int, int]:
    usage = getattr(message, "usage", None)
    return (
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )


def _message_text(message: Any) -> str:
    """Только текстовые блоки: рядом с ними в ответе лежат блоки размышления."""
    parts = [
        block.text
        for block in getattr(message, "content", None) or []
        if getattr(block, "type", "") == "text"
    ]
    return "\n".join(parts).strip()


# --------------------------------------------------------- шаблонный клиент


class TemplateClient:
    """То же письмо, собранное из досье по шаблону: без сети и без ключа.

    Вход — JSON из TemplateClient.payload, а не текст промпта. Промпт
    написан для модели, и вытаскивать факты обратно из его текста значило бы
    ломаться при первой же правке config/prompts/letter.md, который ведёт
    не этот модуль.
    """

    name = "template"

    @staticmethod
    def payload(*, venue: Mapping[str, Any], dossier: Mapping[str, Any]) -> str:
        """Собрать вход для complete(). Форма входа живёт здесь же, рядом
        с тем, кто её читает."""
        return json.dumps({"venue": dict(venue), "dossier": dict(dossier)}, ensure_ascii=False)

    def complete(self, prompt: str) -> str:
        try:
            data = json.loads(prompt)
        except json.JSONDecodeError as exc:
            raise LLMError("TemplateClient ждёт JSON из TemplateClient.payload") from exc
        if not isinstance(data, dict):
            raise LLMError("TemplateClient ждёт JSON-объект из TemplateClient.payload")
        letter = _compose(data.get("venue") or {}, data.get("dossier") or {})
        return json.dumps(letter, ensure_ascii=False)


def _first_name(full_name: Any) -> str:
    """Имя из ФИО — или пустая строка, если порядок слов не определить.

    Русские источники дают и «Имя Фамилия», и «Фамилия Имя», и различить их
    в общем случае нельзя: «Соколов Дмитрий» и «Дмитрий Соколов» одинаково
    вероятны. Обратиться по фамилии («Соколов, вижу...») — заметная ошибка,
    поэтому шаблон здесь молчит: письмо начинается прямо с повода.
    Разбирать ФИО имеет смысл только там, где известен формат источника,
    либо руками — на это указывает gaps.

    Отчество снимает неоднозначность: «Соколов Дмитрий Петрович» — это
    Фамилия Имя Отчество, и имя берётся вторым словом.
    """
    patronymic_endings = ("ович", "евич", "ьич", "овна", "евна", "ична")
    parts = str(full_name or "").split()
    if len(parts) == 3 and parts[2].lower().endswith(patronymic_endings):
        return parts[1]
    return ""


def _confidence(event: Mapping[str, Any], reason: str) -> float:
    if event.get("date") and event.get("attendees"):
        return _CONFIDENCE_FULL
    if event.get("date"):
        return _CONFIDENCE_DATED
    return _CONFIDENCE_REASON if reason else _CONFIDENCE_BLIND


def _subject(event: Mapping[str, Any], venue_name: str) -> str:
    title = str(event.get("title") or "").strip()
    subject = f"{title} — зал в Москве" if title else f"Зал в Москве — {venue_name}"
    if len(subject) <= _MAX_SUBJECT_CHARS:
        return subject
    # Режем по слову: обрубок посреди названия выглядит как сбой рассылки.
    return subject[:_MAX_SUBJECT_CHARS].rsplit(" ", 1)[0].rstrip(" ,—-")


def _venue_line(venue: Mapping[str, Any]) -> str:
    name = str(venue.get("name") or "").strip() or "наша площадка"
    facts = [str(item).strip() for item in venue.get("advantages") or [] if str(item).strip()]
    if facts:
        return f"«{name}» — {', '.join(facts[:_MAX_VENUE_FACTS])}."
    city = str(venue.get("city") or "").strip() or "Москве"
    return f"«{name}» — площадка в {city} под мероприятия такого масштаба."


def _compose(venue: Mapping[str, Any], dossier: Mapping[str, Any]) -> dict[str, Any]:
    """Письмо из досье.

    Порядок предложений задан требованиями к письму, а не вкусом: повод
    идёт первым, вопрос — последним и единственным. Ни одного числа сверх
    тех, что уже есть в досье: выдуманное число участников — не стилистика,
    а сгоревший лид.
    """
    event = dossier.get("event") or {}
    contact = dossier.get("contact") or {}
    reason = str(dossier.get("reason") or "").strip().rstrip(".")

    name = _first_name(contact.get("full_name"))
    if reason:
        lead = f"{name}, вижу, {reason}." if name else f"Вижу, {reason}."
    else:
        lead = f"{name}, пишу насчёт зала под ваше мероприятие." if name else (
            "Пишу насчёт зала под ваше мероприятие."
        )

    repeat = "Если в этом году планируете так же" if event.get("date") else (
        "Если под это планируете мероприятие"
    )
    timing = f"{repeat}, сейчас как раз время смотреть залы: на такие даты их разбирают заранее."

    body = "\n\n".join([f"{lead} {timing}", _venue_line(venue), "Прислать свободные даты?"])
    return {
        "subject": _subject(event, str(venue.get("name") or "").strip() or "площадка"),
        "body": body,
        "hook": reason or "повода в досье нет",
        "confidence": _confidence(event, reason),
    }


# ------------------------------------------------------------- выбор клиента


def get_client(settings: Settings | None = None) -> LLMClient:
    """Ключ есть — пишет модель, ключа нет — шаблон.

    Переключение логируется: когда в письмах вдруг окажется один и тот же
    текст, причина должна находиться за десять секунд.
    """
    settings = settings or get_settings()
    if not settings.anthropic_api_key:
        get_logger("gtm.generate.llm").info("llm.template", reason="no_key")
        return TemplateClient()
    return AnthropicClient(
        settings.anthropic_api_key,
        model=settings.llm_model,
        max_tokens=settings.llm_max_tokens,
    )
