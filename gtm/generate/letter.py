"""Письмо — четвёртое звено.

Здесь у системы редкая роскошь: повод железобетонный и конкретный.
«Вижу, в прошлом ноябре вы проводили конференцию на 600 человек в такой-то
площадке; если в этом году планируете, сейчас как раз время смотреть залы».
Поэтому модуль занят не убедительностью текста, а обратным — тем, чтобы
письмо не сказало того, чего в досье нет.

Два правила, которые важнее красоты:

1. Промпт живёт в config/prompts/letter.md и правится без кода. Модуль его
   только читает.
2. Всё, что вернул клиент, проходит validate_letter. Проверка дешёвая и
   не блокирующая: нарушения уходят в StageResult.details, потому что читать
   их будет тот, кто правит промпт, а не тот, кто читает логи.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from typing import Any

import jinja2
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy.orm import Session

from gtm.config import Config, load_prompt
from gtm.generate.llm import (
    AnthropicClient,
    LLMClient,
    LLMError,
    LLMPermanentError,
    TemplateClient,
    get_client,
)
from gtm.observability import StageResult, get_logger, stage
from gtm.spend import BudgetExceeded, SpendGuard
from gtm.storage import repo
from gtm.storage.models import ExpectationStatus, OutreachStatus

STAGE_NAME = "generate"
PROMPT_NAME = "letter.md"

# Требования к письму. Дублируют правила из config/prompts/letter.md:
# там они объясняются модели, здесь — проверяются на результате. Править
# нужно вместе, иначе проверка начнёт ловить то, о чём модель не просили.
MAX_WORDS = 120
MAX_SUBJECT_CHARS = 60
BANNED_PHRASES = (
    "надеюсь, у вас всё хорошо",
    "в современном мире",
    "уникальное пространство",
    "команда профессионалов",
)

# Ниже этого письмо менеджеру не показываем: слабый повод хуже отсутствия
# письма — он сжигает адрес, к которому потом не вернуться.
MIN_LETTER_CONFIDENCE = 0.3

# Технические константы разбора.
# Слово — любая последовательность букв или цифр; знаки препинания не в счёт.
_WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")
_DIGITS_RE = re.compile(r"\d+")
# Граница предложения — точка/восклицательный/вопросительный плюс пробел.
# Без пробела не режем: «01.06.2026» это одна дата, а не три предложения.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
# Значимое слово повода: короткие предлоги и союзы якорем быть не могут.
_ANCHOR_MIN_LEN = 5
_SNIPPET_CHARS = 200

_ENV = jinja2.Environment(autoescape=False, keep_trailing_newline=True)


class LetterParseError(ValueError):
    """Ответ клиента не удалось прочитать как письмо."""


# ------------------------------------------------------------------ письмо


class Letter(BaseModel):
    """Письмо в том виде, в каком его возвращает модель."""

    model_config = ConfigDict(extra="ignore")

    subject: str
    body: str
    hook: str = ""
    confidence: float = 0.0

    @field_validator("subject", "body", "hook", mode="before")
    @classmethod
    def _as_text(cls, value: Any) -> Any:
        return "" if value is None else str(value).strip()

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, value: Any) -> Any:
        # Модель иногда возвращает 1.2 или строку. Из-за одной цифры терять
        # готовое письмо не стоит — обрезаем в допустимый диапазон.
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        return min(1.0, max(0.0, number))


# ------------------------------------------------------------------ промпт


def _clean(value: Any) -> dict[str, Any]:
    """Словарь без пустых значений.

    Шаблон промпта — чужой файл, и он не обязан проверять каждое поле.
    Пустые выкидываем здесь, чтобы в тексте не появилось «участников ~None»:
    для модели это выглядит как факт.
    """
    if not isinstance(value, Mapping):
        return {}
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


def _prompt_context(config: Config, dossier: Mapping[str, Any]) -> dict[str, Any]:
    """Переменные шаблона: venue, company, evidence, contact."""
    event = _clean(dossier.get("event"))
    evidence: dict[str, Any] = {"reason": str(dossier.get("reason") or "").strip()}
    # Прошлое мероприятие без даты — не повод, а догадка: в промпт не идёт.
    if event.get("date"):
        evidence["previous_event"] = event
    return {
        "venue": config.icp.venue,
        "company": _clean(dossier.get("company")),
        "contact": _clean(dossier.get("contact")),
        "evidence": evidence,
    }


def render_prompt(config: Config, dossier: dict[str, Any]) -> str:
    """Промпт из config/prompts/letter.md, заполненный данными досье."""
    template = _ENV.from_string(load_prompt(PROMPT_NAME))
    return template.render(**_prompt_context(config, dossier)).strip()


# ------------------------------------------------------------------ разбор


def _balanced_object(text: str) -> str | None:
    """Первый сбалансированный {...}.

    Регулярным выражением это не берётся: внутри строк JSON встречаются
    фигурные скобки, а модель любит обрамлять ответ пояснениями.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _json_candidates(raw: str) -> Iterator[str]:
    """Что в ответе может оказаться JSON-ом — от самого надёжного варианта
    к самому терпимому: блок кода, весь текст, первый объект в тексте."""
    text = (raw or "").strip()
    for match in _FENCE_RE.finditer(text):
        block = match.group(1).strip()
        if block:
            yield block
            nested = _balanced_object(block)
            if nested is not None and nested != block:
                yield nested
    if text:
        yield text
    inner = _balanced_object(text)
    if inner is not None and inner != text:
        yield inner


def _snippet(raw: str) -> str:
    text = " ".join((raw or "").split())
    return text[:_SNIPPET_CHARS] or "<пусто>"


def parse_letter(raw: str) -> Letter:
    """Письмо из ответа клиента.

    Ответ приходит JSON-ом, но иногда в markdown-блоке кода, иногда
    с пояснением до и после — поэтому JSON вынимается устойчиво.
    """
    for candidate in _json_candidates(raw):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        try:
            return Letter.model_validate(data)
        except ValueError as exc:
            raise LetterParseError(
                f"в ответе есть JSON, но это не письмо: {exc}"
            ) from exc
    raise LetterParseError(f"в ответе клиента нет JSON-объекта письма: {_snippet(raw)}")


# ------------------------------------------------------------- проверка письма


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text)


def _normalized(text: str) -> str:
    """Текст для сравнения оборотов: без регистра, без «ё», без пунктуации."""
    return " ".join(_words(text.lower().replace("ё", "е")))


def _numbers(text: str) -> set[str]:
    """Числа как строки, без ведущих нулей: «06» и «6» — одно и то же."""
    return {group.lstrip("0") or "0" for group in _DIGITS_RE.findall(text)}


def _dossier_numbers(value: Any) -> set[str]:
    """Все числа, которые в досье есть — в любом поле и на любой глубине."""
    if isinstance(value, Mapping):
        return set().union(*(_dossier_numbers(item) for item in value.values()), set())
    if isinstance(value, list | tuple):
        return set().union(*(_dossier_numbers(item) for item in value), set())
    if isinstance(value, bool) or value is None:
        return set()
    if isinstance(value, float):
        return _numbers(f"{value:.0f}")
    if isinstance(value, int | str):
        return _numbers(str(value))
    return set()


def _first_sentence(body: str) -> str:
    parts = [part for part in _SENTENCE_RE.split(body.strip()) if part.strip()]
    return parts[0] if parts else ""


def _anchors(reason: str) -> set[str]:
    """Слова, по которым видно, что письмо начинается с факта из досье,
    а не с «мы заметили, что ваша компания активно развивается»."""
    return {
        word
        for word in _words(reason.lower().replace("ё", "е"))
        if len(word) >= _ANCHOR_MIN_LEN or word.isdigit()
    }


def validate_letter(letter: Letter, dossier: Mapping[str, Any]) -> list[str]:
    """Чем письмо нарушает требования. Пустой список — всё в порядке.

    Проверка не блокирует отправку: она пишется в StageResult.details и
    в карточку касания. Это дешёвая защита от того, что модель начнёт
    выдумывать, а не арбитр качества текста.
    """
    violations: list[str] = []
    body = letter.body.strip()
    subject = letter.subject.strip()
    if not body:
        return ["пустое письмо"]
    if not subject:
        violations.append("пустая тема")
    elif len(subject) > MAX_SUBJECT_CHARS:
        violations.append(f"тема длиннее {MAX_SUBJECT_CHARS} символов: {len(subject)}")

    words = len(_words(body))
    if words > MAX_WORDS:
        violations.append(f"длиннее {MAX_WORDS} слов: {words}")

    anchors = _anchors(str(dossier.get("reason") or ""))
    if anchors and not anchors & set(_words(_first_sentence(body).lower().replace("ё", "е"))):
        violations.append("повода нет в первом предложении")

    questions = body.count("?")
    if questions != 1:
        violations.append(f"вопросов не ровно один: {questions}")
    elif not body.endswith("?"):
        violations.append("вопрос не в конце письма")

    # Числа о заказчике письмо берёт только из досье. Про площадку менеджер
    # расскажет сам — выдуманная цифра в первом касании стоит дороже.
    invented = sorted(_numbers(f"{subject} {body}") - _dossier_numbers(dossier))
    if invented:
        violations.append(f"числа, которых нет в досье: {', '.join(invented)}")

    text = _normalized(f"{subject} {body}")
    banned = [phrase for phrase in BANNED_PHRASES if _normalized(phrase) in text]
    if banned:
        violations.append(f"запрещённые обороты: {'; '.join(banned)}")
    return violations


# ------------------------------------------------------------------ генерация


def _venue_block(config: Config) -> dict[str, Any]:
    venue = config.icp.venue
    return {"name": venue.name, "city": venue.city, "advantages": list(venue.advantages)}


def _client_input(config: Config, dossier: dict[str, Any], client: LLMClient) -> str:
    """Что уходит клиенту: живой модели — промпт, шаблонному — сами факты."""
    if isinstance(client, TemplateClient):
        return TemplateClient.payload(venue=_venue_block(config), dossier=dossier)
    return render_prompt(config, dossier)


def generate_letter(config: Config, dossier: dict[str, Any], *, client: LLMClient) -> Letter:
    """Одно письмо по одному досье."""
    return parse_letter(client.complete(_client_input(config, dossier, client)))


# ------------------------------------------------------------------- стадия


def _min_confidence(config: Config) -> float:
    """Порог показа письма менеджеру.

    Читаем из config/signals.yaml (scoring.min_letter_confidence): это
    бизнес-параметр, и его место в конфиге. Пока его там нет — работает
    умолчание модуля.
    """
    value = getattr(config.signals.scoring, "min_letter_confidence", None)
    try:
        return float(value)
    except (TypeError, ValueError):
        return MIN_LETTER_CONFIDENCE


def _notes(letter: Letter, violations: list[str]) -> str:
    """Заметка к касанию: повод, уверенность и претензии к тексту — всё,
    что менеджеру нужно решить, отправлять письмо или переписать."""
    lines = [
        f"повод: {letter.hook or 'не назван'}",
        f"уверенность: {letter.confidence:.2f}",
    ]
    if violations:
        lines.append("нарушения: " + "; ".join(violations))
    return "\n".join(lines)


def _count_violations(result: StageResult, violations: list[str]) -> None:
    result.note("with_violations")
    codes = result.details.setdefault("violation_codes", {})
    for item in violations:
        # Ключ — вид нарушения без подробностей: конкретика уезжает в заметку
        # к письму, а в сводке прогона нужен счётчик, а не список.
        code = item.split(":", 1)[0]
        codes[code] = codes.get(code, 0) + 1


def run_generate(
    session: Session,
    config: Config,
    *,
    run_id: str,
    limit: int | None = None,
    client: LLMClient | None = None,
) -> StageResult:
    """Написать письма по обогащённым ожиданиям — сверху вниз по score.

    Стоп-лист проверяется здесь заново: между фильтром и генерацией мог
    пройти день, а отписка приходит в любой из них.
    """
    log = get_logger("gtm.generate")
    # repo трактует limit=0 как «без лимита». Для стадии, которая тратит
    # деньги, `--limit 0` обязан означать «ничего», а не «всё».
    targets = (
        []
        if limit is not None and limit <= 0
        else list(
            repo.expectations_by_status(session, [ExpectationStatus.ENRICHED.value], limit=limit)
        )
    )
    if not targets:
        # Клиент модели не создаётся, когда писать нечего. Иначе прогон,
        # у которого просто нет работы на сегодня, падает на отсутствующей
        # необязательной зависимости — а это штатное утро, не авария.
        with stage(session, run_id, STAGE_NAME, count_in=0) as result:
            result.note("nothing_to_write")
        log.info("generate.nothing_to_do")
        return result

    client = client or get_client()
    min_confidence = _min_confidence(config)

    with stage(session, run_id, STAGE_NAME, count_in=len(targets)) as result:
        result.details["client"] = client.name
        if isinstance(client, AnthropicClient):
            # Клиент строится без сессии (get_client), а трата обязана попасть
            # в ту же сессию, что и стадия — поэтому журнал цепляем здесь.
            client.guard = SpendGuard(session, stage=STAGE_NAME)

        handled = 0
        for expectation in targets:
            contact = repo.primary_contact(session, expectation.inn)
            email = contact.email if contact else None
            if repo.is_suppressed(session, inn=expectation.inn, email=email):
                repo.set_status(session, expectation, ExpectationStatus.FILTERED_OUT.value)
                result.note("suppressed")
                handled += 1
                continue

            dossier = expectation.dossier or {}
            if not dossier:
                # Без досье письму не на что опереться, а повторять попытку
                # каждый день незачем: сначала обогащение.
                repo.set_status(session, expectation, ExpectationStatus.SNOOZED.value)
                result.note("no_dossier")
                handled += 1
                continue

            try:
                letter = generate_letter(config, dossier, client=client)
            except BudgetExceeded as exc:
                result.details["stopped"] = "budget_exceeded"
                result.details["stop_reason"] = str(exc)
                result.details["not_generated"] = len(targets) - handled
                log.warning(
                    "generate.budget_exceeded",
                    run_id=run_id,
                    written=result.count_out,
                    left=len(targets) - handled,
                    error=str(exc),
                )
                break
            except LLMPermanentError as exc:
                # Опечатка в ключе или несуществующая модель: на следующих
                # записях будет то же самое. Останавливаемся, чтобы в логе
                # осталась одна внятная причина, а не сорок одинаковых.
                result.details["stop_reason"] = str(exc)
                result.details["not_generated"] = len(targets) - handled
                log.error(
                    "generate.stopped",
                    run_id=run_id,
                    written=result.count_out,
                    left=len(targets) - handled,
                    error=str(exc),
                )
                break
            except (LetterParseError, LLMError) as exc:
                # Одно нечитаемое письмо не должно уносить весь прогон.
                repo.set_status(session, expectation, ExpectationStatus.SNOOZED.value)
                result.note("generation_failed")
                handled += 1
                log.warning("generate.failed", inn=expectation.inn, error=str(exc))
                continue

            handled += 1
            violations = validate_letter(letter, dossier)
            if violations:
                _count_violations(result, violations)

            weak = letter.confidence < min_confidence
            outreach = repo.create_outreach(
                session,
                expectation_id=expectation.id,
                contact_id=contact.id if contact else None,
                subject=letter.subject,
                body=letter.body,
                generated_by=client.name,
                # Слабое письмо сохраняем, но помечаем как пропущенное: по нему
                # потом видно, на каких поводах модель проседает.
                status=(
                    OutreachStatus.SKIPPED.value if weak else OutreachStatus.DRAFT.value
                ),
            )
            outreach.notes = _notes(letter, violations)

            if weak:
                repo.set_status(session, expectation, ExpectationStatus.SNOOZED.value)
                result.note("low_confidence")
                result.details["low_confidence_reason"] = (
                    f"уверенность ниже {min_confidence}: менеджеру не показываем"
                )
            else:
                repo.set_status(session, expectation, ExpectationStatus.DRAFTED.value)
                result.bump_out()
        session.flush()
    return result
