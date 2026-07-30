"""Письмо: что в нём должно оказаться и чего в нём быть не может."""

from __future__ import annotations

import builtins
import json
import re
from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from gtm.generate import (
    AnthropicClient,
    Letter,
    LetterParseError,
    TemplateClient,
    estimate_cost_rub,
    generate_letter,
    parse_letter,
    render_prompt,
    run_generate,
    validate_letter,
)
from gtm.storage import repo
from gtm.storage.models import ApiSpend, ExpectationStatus, Outreach, SuppressionReason

# ------------------------------------------------------------------ фикстуры


def _dossier(**overrides: Any) -> dict[str, Any]:
    """Досье в том виде, в каком его отдаёт стадия обогащения."""
    data: dict[str, Any] = {
        "company": {
            "inn": "7701234567",
            "name": "ООО Ромашка",
            "headcount": 900,
            "revenue_last": 1_500_000_000.0,
            "registered_at": "2011-03-15",
            "city": "Москва",
            "age_years": 15,
        },
        "event": {
            "title": "Итоги года",
            "date": "2025-11-20",
            "attendees": 600,
            "venue": "Крокус Экспо",
            "is_repeat": True,
            "years_repeated": 2,
        },
        "contact": {
            "full_name": "Анна Петрова",
            "position": "event-менеджер",
            "email": "a.petrova@romashka.ru",
            "is_generic": False,
        },
        "reason": "в ноябре 2025 проводили «Итоги года» на ~600 человек в Крокус Экспо",
        "gaps": [],
        "enriched_at": "2026-07-29T09:00:00+00:00",
    }
    data.update(overrides)
    return data


def _enriched(session, *, inn: str = "7701234567", score: float = 0.7, dossier=None):
    expectation, _ = repo.upsert_expectation(
        session,
        inn=inn,
        kind="event_anniversary",
        expected_at=date(2026, 11, 20),
        window_opens_at=date(2026, 7, 23),
        window_closes_at=date(2026, 9, 21),
        confidence=0.8,
        expected_attendees=600,
    )
    expectation.status = ExpectationStatus.ENRICHED.value
    expectation.score = score
    expectation.dossier = _dossier() if dossier is None else dossier
    session.flush()
    return expectation


class _StubClient:
    """Клиент, который возвращает заранее заданный ответ. Нужен там, где
    проверяется поведение стадии, а не текст письма."""

    name = "stub"

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.payload


def _letter_json(**overrides: Any) -> str:
    data = {
        "subject": "Итоги года — зал в Москве",
        "body": (
            "Анна, вижу, в ноябре 2025 проводили «Итоги года» на ~600 человек.\n\n"
            "Прислать свободные даты?"
        ),
        "hook": "прошлогодняя конференция",
        "confidence": 0.9,
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


# -------------------------------------------------------------------- промпт


def test_prompt_carries_the_dossier(config):
    prompt = render_prompt(config, _dossier())

    assert "Союз Холл" in prompt
    assert "ООО Ромашка" in prompt
    assert "900" in prompt  # штат из досье
    assert "в ноябре 2025 проводили" in prompt
    assert "Крокус Экспо" in prompt
    assert "Анна Петрова" in prompt
    assert "event-менеджер" in prompt


def test_prompt_omits_unknown_fields_instead_of_printing_none(config):
    dossier = _dossier(
        company={"inn": "7701234567", "name": "ООО Ромашка", "headcount": None},
        event={"title": "Итоги года", "date": "2025-11-20", "attendees": None, "venue": None},
        contact={"full_name": None, "position": None, "email": None, "is_generic": None},
        reason="в ноябре 2025 проводили «Итоги года»",
    )

    prompt = render_prompt(config, dossier)

    # «участников ~None» модель прочитает как факт — такого в промпте быть не может.
    assert "None" not in prompt
    assert "Крокус" not in prompt
    assert "в ноябре 2025 проводили" in prompt


def test_prompt_without_past_event_has_no_event_block(config):
    dossier = _dossier(
        event={"title": None, "date": None, "attendees": None, "venue": None},
        reason="в марте 2026 компании исполняется 15 лет",
    )

    prompt = render_prompt(config, dossier)

    assert "Прошлое мероприятие" not in prompt
    assert "в марте 2026 компании исполняется 15 лет" in prompt


# -------------------------------------------------------------------- разбор


def test_parse_letter_reads_plain_json():
    letter = parse_letter(_letter_json())

    assert letter.subject == "Итоги года — зал в Москве"
    assert letter.confidence == 0.9


def test_parse_letter_reads_json_in_a_code_fence():
    raw = f"```json\n{_letter_json()}\n```"

    assert parse_letter(raw).hook == "прошлогодняя конференция"


def test_parse_letter_reads_json_surrounded_by_chatter():
    raw = (
        "Конечно! Вот письмо, я постарался уложиться в 120 слов:\n\n"
        f"{_letter_json()}\n\n"
        "Если нужно короче — скажите."
    )

    assert parse_letter(raw).subject == "Итоги года — зал в Москве"


def test_parse_letter_survives_braces_inside_strings():
    raw = _letter_json(body="Текст со скобкой } и ещё одной { внутри. Прислать даты?")

    assert parse_letter(raw).body.endswith("Прислать даты?")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "Извините, не могу помочь.",
        '{"subject": "тема"',  # обрезано по лимиту токенов
    ],
)
def test_parse_letter_raises_a_readable_error(raw):
    with pytest.raises(LetterParseError):
        parse_letter(raw)


def test_parse_letter_rejects_json_without_body():
    with pytest.raises(LetterParseError, match="не письмо"):
        parse_letter('{"subject": "тема", "confidence": 0.9}')


def test_confidence_out_of_range_is_clamped_not_fatal():
    assert parse_letter(_letter_json(confidence=1.7)).confidence == 1.0
    assert parse_letter(_letter_json(confidence="нет данных")).confidence == 0.0


# ------------------------------------------------------------ шаблонный клиент


def test_template_letter_passes_validation(config):
    dossier = _dossier()

    letter = generate_letter(config, dossier, client=TemplateClient())

    assert validate_letter(letter, dossier) == []
    assert letter.confidence >= 0.9
    # «Анна Ковалёва» — порядок слов неизвестен, обращения по имени быть не должно:
    # ошибиться фамилией хуже, чем начать прямо с повода.
    assert letter.body.startswith("Вижу, в ноябре 2025 проводили")
    assert letter.body.rstrip().endswith("?")
    assert "Союз Холл" in letter.body


def test_template_greets_by_name_only_when_patronymic_disambiguates(config):
    """Отчество однозначно указывает порядок «Фамилия Имя Отчество»."""
    letter = generate_letter(
        config,
        _dossier(contact={"full_name": "Соколов Дмитрий Петрович", "email": "d@x.example"}),
        client=TemplateClient(),
    )

    assert letter.body.startswith("Дмитрий, вижу,")


def test_template_letter_without_attendees_has_no_headcount_digits(config):
    dossier = _dossier(
        event={"title": "Итоги года", "date": "2025-11-20", "attendees": None, "venue": None},
        reason="в ноябре 2025 проводили «Итоги года»",
    )

    letter = generate_letter(config, dossier, client=TemplateClient())

    assert validate_letter(letter, dossier) == []
    # В письме остаётся только год из повода — числа участников там взяться неоткуда.
    assert set(re.findall(r"\d+", f"{letter.subject} {letter.body}")) == {"2025"}
    assert "600" not in letter.body


def test_template_letter_without_a_reason_is_not_confident(config):
    dossier = _dossier(
        event={"title": None, "date": None, "attendees": None, "venue": None},
        reason="",
    )

    letter = generate_letter(config, dossier, client=TemplateClient())

    # Повода нет — такому письму система не даст дойти до менеджера.
    assert letter.confidence < 0.3
    assert validate_letter(letter, dossier) == []


def test_template_letter_greets_without_a_name_when_contact_is_unknown(config):
    dossier = _dossier(contact={"full_name": None, "position": None, "email": None})

    letter = generate_letter(config, dossier, client=TemplateClient())

    assert letter.body.startswith("Вижу, в ноябре 2025 проводили")
    assert validate_letter(letter, dossier) == []


# ------------------------------------------------------------------ проверка


def test_validation_catches_an_invented_number():
    dossier = _dossier()
    letter = Letter(
        subject="Тема",
        body=(
            "Вижу, в ноябре 2025 проводили «Итоги года» на ~600 человек. "
            "У нас зал на 1500 гостей. Прислать даты?"
        ),
        confidence=0.9,
    )

    violations = validate_letter(letter, dossier)

    assert violations == ["числа, которых нет в досье: 1500"]


def test_validation_catches_banned_phrases_and_two_questions():
    dossier = _dossier()
    letter = Letter(
        subject="Тема",
        body=(
            "Вижу, в ноябре 2025 проводили «Итоги года». Надеюсь, у Вас все хорошо! "
            "Наше уникальное пространство ждёт вас. Интересно? Прислать даты?"
        ),
        confidence=0.9,
    )

    violations = validate_letter(letter, dossier)

    assert "вопросов не ровно один: 2" in violations
    assert any("надеюсь, у вас всё хорошо" in item for item in violations)
    assert any("уникальное пространство" in item for item in violations)


def test_validation_catches_a_missing_hook_and_a_long_letter():
    dossier = _dossier()
    filler = " ".join(["слово"] * 130)
    letter = Letter(
        subject="Тема",
        body=f"Мы заметили, что ваша компания активно развивается. {filler} Прислать даты?",
        confidence=0.9,
    )

    violations = validate_letter(letter, dossier)

    assert "повода нет в первом предложении" in violations
    assert any(item.startswith("длиннее 120 слов") for item in violations)


def test_validation_catches_a_question_that_is_not_last():
    dossier = _dossier()
    letter = Letter(
        subject="Тема",
        body="Вижу, в ноябре 2025 проводили «Итоги года». Прислать даты? Ждём ответа.",
        confidence=0.9,
    )

    assert "вопрос не в конце письма" in validate_letter(letter, dossier)


def test_validation_of_an_empty_letter_stops_at_the_first_problem():
    assert validate_letter(Letter(subject="Тема", body="   ", confidence=0.5), _dossier()) == [
        "пустое письмо"
    ]


# ------------------------------------------------------------------- стадия


def test_run_generate_drafts_a_letter(session, config, company_factory):
    company_factory("7701234567")
    repo.upsert_contact(
        session, inn="7701234567", email="a.petrova@romashka.ru", full_name="Анна Петрова"
    )
    expectation = _enriched(session)

    result = run_generate(session, config, run_id="run-1", client=TemplateClient())

    assert (result.count_in, result.count_out) == (1, 1)
    assert expectation.status == ExpectationStatus.DRAFTED.value
    outreach = session.scalars(select(Outreach)).one()
    assert outreach.status == "draft"
    assert outreach.expectation_id == expectation.id
    assert outreach.contact_id is not None
    assert outreach.generated_by == "template"
    assert "Прислать свободные даты?" in outreach.body
    assert "уверенность: 0.90" in outreach.notes
    # Стадия обязана попадать в сводку прогона.
    assert {row["stage"]: row["out"] for row in repo.run_summary(session, "run-1")}["generate"] == 1


def test_run_generate_respects_limit_and_score_order(session, config, company_factory):
    for inn, score in (("7701234567", 0.1), ("7702345678", 0.9), ("7703456789", 0.5)):
        company_factory(inn)
        _enriched(session, inn=inn, score=score)

    result = run_generate(session, config, run_id="run-2", limit=2, client=TemplateClient())

    assert (result.count_in, result.count_out) == (2, 2)
    statuses = {
        e.inn: e.status
        for e in repo.expectations_by_status(
            session, [ExpectationStatus.ENRICHED.value, ExpectationStatus.DRAFTED.value]
        )
    }
    assert statuses["7702345678"] == ExpectationStatus.DRAFTED.value
    assert statuses["7703456789"] == ExpectationStatus.DRAFTED.value
    assert statuses["7701234567"] == ExpectationStatus.ENRICHED.value


def test_zero_limit_writes_nothing(session, config, company_factory):
    company_factory("7701234567")
    expectation = _enriched(session)

    result = run_generate(session, config, run_id="run-3", limit=0, client=TemplateClient())

    assert (result.count_in, result.count_out) == (0, 0)
    assert expectation.status == ExpectationStatus.ENRICHED.value
    assert session.scalars(select(Outreach)).all() == []


def test_empty_run_does_not_build_a_client(session, config, monkeypatch):
    """Прогон, у которого нет работы на сегодня, — штатное утро, а не авария.

    Раньше клиент модели создавался до проверки, есть ли что писать, и такой
    прогон падал на отсутствующем пакете anthropic: ключ задан, зависимость
    из необязательной группы не поставлена, писать при этом всё равно нечего.
    """
    from gtm.generate import letter as letter_module

    def explode() -> None:
        raise AssertionError("клиент не должен создаваться, когда писать нечего")

    monkeypatch.setattr(letter_module, "get_client", explode)

    result = run_generate(session, config, run_id="run-empty")

    assert (result.count_in, result.count_out) == (0, 0)
    assert result.details["nothing_to_write"] == 1


def test_missing_sdk_error_points_at_the_right_command(monkeypatch):
    """Сообщение обязано вести к рабочей команде. `pip install 'gtm[llm]'`
    тянет с PyPI чужой пакет — точка в '.[llm]' здесь принципиальна."""
    from gtm.generate.llm import LLMError, _build_sdk

    real_import = builtins.__import__

    def no_anthropic(name, *args, **kwargs):
        if name == "anthropic":
            raise ModuleNotFoundError("No module named 'anthropic'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_anthropic)

    with pytest.raises(LLMError) as excinfo:
        _build_sdk("sk-test")

    message = str(excinfo.value)
    assert "-e '.[llm]'" in message
    assert "'gtm[llm]'" not in message
    # И путь отступления: без ключа система работает шаблонным генератором.
    assert "шаблонный" in message


class _RejectingClient:
    """Клиент, который отвечает как провайдер с отклонённым ключом."""

    name = "anthropic/тест"

    def __init__(self, status: int) -> None:
        self.status = status
        self.calls = 0

    def complete(self, prompt: str) -> str:
        from gtm.generate.llm import LLMError, LLMPermanentError, _is_permanent

        self.calls += 1
        exc = RuntimeError(f"Error code: {self.status}")
        exc.status_code = self.status
        if _is_permanent(exc):
            raise LLMPermanentError("anthropic: ключ отклонён провайдером")
        raise LLMError("anthropic: временный сбой")


def test_rejected_key_stops_the_stage_after_one_call(session, config, company_factory):
    """Опечатка в ключе не исправится на следующей записи. Без остановки одна
    опечатка даёт столько одинаковых ошибок, сколько собрано досье, и настоящую
    причину в логе уже не видно."""
    for inn in ("7701234567", "7703456789", "7712345678"):
        company_factory(inn)
        _enriched(session, inn=inn)
    client = _RejectingClient(401)

    result = run_generate(session, config, run_id="run-401", client=client)

    assert client.calls == 1, "второй записи быть не должно"
    assert result.count_out == 0
    assert result.details["not_generated"] == 3
    assert "ключ отклонён" in result.details["stop_reason"]


def test_transient_failure_keeps_going(session, config, company_factory):
    """Временный сбой — другое дело: следующую запись пробуем."""
    for inn in ("7701234567", "7703456789"):
        company_factory(inn)
        _enriched(session, inn=inn)
    client = _RejectingClient(503)

    result = run_generate(session, config, run_id="run-503", client=client)

    assert client.calls == 2, "по каждой записи своя попытка"
    assert result.details["generation_failed"] == 2
    assert "stop_reason" not in result.details


@pytest.mark.parametrize(
    ("status", "permanent"),
    [(400, True), (401, True), (403, True), (404, True), (422, True), (429, False), (500, False),
     (503, False), (None, False)],
)
def test_permanent_statuses(status, permanent):
    """429 и 5xx временные намеренно: они лечатся повтором, а 4xx нет."""
    from gtm.generate.llm import _is_permanent

    exc = RuntimeError("сбой")
    if status is not None:
        exc.status_code = status
    assert _is_permanent(exc) is permanent


def test_status_is_read_from_response_too():
    """SDK кладёт код то в исключение, то в его response."""
    from gtm.generate.llm import _is_permanent

    exc = RuntimeError("сбой")
    exc.response = type("R", (), {"status_code": 401})()
    assert _is_permanent(exc) is True


def test_run_generate_touches_only_enriched(session, config, company_factory):
    company_factory("7701234567")
    expectation = _enriched(session)
    expectation.status = ExpectationStatus.READY.value
    session.flush()

    result = run_generate(session, config, run_id="run-4", client=TemplateClient())

    assert (result.count_in, result.count_out) == (0, 0)
    assert expectation.status == ExpectationStatus.READY.value


def test_suppression_blocks_generation(session, config, company_factory):
    company_factory("7701234567")
    expectation = _enriched(session)
    # Стоп-лист мог пополниться уже после фильтра — проверяем ещё раз здесь.
    repo.add_suppression(session, reason=SuppressionReason.OPTED_OUT.value, inn="7701234567")
    client = _StubClient(_letter_json())

    result = run_generate(session, config, run_id="run-5", client=client)

    assert (result.count_in, result.count_out) == (1, 0)
    assert result.details["suppressed"] == 1
    assert expectation.status == ExpectationStatus.FILTERED_OUT.value
    assert client.prompts == []  # до генерации дело не дошло
    assert session.scalars(select(Outreach)).all() == []


def test_suppressed_email_blocks_generation_too(session, config, company_factory):
    company_factory("7701234567")
    repo.upsert_contact(session, inn="7701234567", email="a.petrova@romashka.ru")
    expectation = _enriched(session)
    repo.add_suppression(
        session, reason=SuppressionReason.BOUNCED.value, email="a.petrova@romashka.ru"
    )

    run_generate(session, config, run_id="run-6", client=TemplateClient())

    assert expectation.status == ExpectationStatus.FILTERED_OUT.value


def test_low_confidence_goes_to_snoozed(session, config, company_factory):
    company_factory("7701234567")
    expectation = _enriched(session)
    client = _StubClient(_letter_json(confidence=0.15))

    result = run_generate(session, config, run_id="run-7", client=client)

    assert (result.count_in, result.count_out) == (1, 0)
    assert expectation.status == ExpectationStatus.SNOOZED.value
    assert result.details["low_confidence"] == 1
    assert "уверенность ниже" in result.details["low_confidence_reason"]
    # Черновик не выбрасываем, но и менеджеру не показываем.
    outreach = session.scalars(select(Outreach)).one()
    assert outreach.status == "skipped"


def test_violations_land_in_stage_details(session, config, company_factory):
    company_factory("7701234567")
    _enriched(session)
    client = _StubClient(
        _letter_json(
            body=(
                "Вижу, в ноябре 2025 проводили «Итоги года». "
                "Наше уникальное пространство вмещает 1500 гостей. Прислать даты?"
            )
        )
    )

    result = run_generate(session, config, run_id="run-8", client=client)

    assert result.count_out == 1  # нарушения не блокируют письмо
    assert result.details["with_violations"] == 1
    assert result.details["violation_codes"] == {
        "числа, которых нет в досье": 1,
        "запрещённые обороты": 1,
    }
    assert "нарушения:" in session.scalars(select(Outreach)).one().notes


def test_unreadable_answer_snoozes_one_expectation_not_the_run(
    session, config, company_factory
):
    for inn, score in (("7701234567", 0.9), ("7702345678", 0.4)):
        company_factory(inn)
        _enriched(session, inn=inn, score=score)

    class _FlakyClient:
        name = "flaky"

        def __init__(self) -> None:
            self.calls = 0

        def complete(self, prompt: str) -> str:
            self.calls += 1
            return "Не могу помочь." if self.calls == 1 else _letter_json()

    result = run_generate(session, config, run_id="run-9", client=_FlakyClient())

    assert (result.count_in, result.count_out) == (2, 1)
    assert result.details["generation_failed"] == 1
    statuses = {
        e.inn: e.status
        for e in repo.expectations_by_status(
            session, [ExpectationStatus.SNOOZED.value, ExpectationStatus.DRAFTED.value]
        )
    }
    assert statuses["7701234567"] == ExpectationStatus.SNOOZED.value
    assert statuses["7702345678"] == ExpectationStatus.DRAFTED.value


def test_expectation_without_dossier_is_snoozed(session, config, company_factory):
    company_factory("7701234567")
    expectation = _enriched(session, dossier={})

    result = run_generate(session, config, run_id="run-10", client=TemplateClient())

    assert result.details["no_dossier"] == 1
    assert expectation.status == ExpectationStatus.SNOOZED.value


# --------------------------------------------------------------- учёт трат


class _FakeUsage:
    input_tokens = 1200
    output_tokens = 300


class _FakeBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeMessage:
    stop_reason = "end_turn"

    def __init__(self, text: str) -> None:
        self.content = [_FakeBlock(text)]
        self.usage = _FakeUsage()


class _FakeMessages:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeMessage:
        self.calls.append(kwargs)
        return _FakeMessage(self.text)


class _FakeSdk:
    """Anthropic SDK в объёме, который нужен клиенту. В сеть не ходит."""

    def __init__(self, text: str) -> None:
        self.messages = _FakeMessages(text)


def test_llm_spend_is_logged(session, config, company_factory):
    company_factory("7701234567")
    _enriched(session)
    sdk = _FakeSdk(f"```json\n{_letter_json()}\n```")
    client = AnthropicClient("test-key", model="claude-opus-5", max_tokens=1200, client=sdk)

    result = run_generate(session, config, run_id="run-11", client=client)

    assert result.count_out == 1
    spend = session.scalars(select(ApiSpend)).one()
    assert spend.provider == "anthropic"
    assert spend.stage == "generate"
    assert spend.ok is True
    assert spend.units == 1500
    assert spend.cost_rub == pytest.approx(estimate_cost_rub(1200, 300))
    assert spend.cost_rub > 0
    # Модель попадает в generated_by: по нему потом видно, чем написано письмо.
    assert session.scalars(select(Outreach)).one().generated_by == "anthropic/claude-opus-5"
    # Промпт уходит из config/prompts/letter.md, а не из шаблона.
    assert "Каркас письма" in sdk.messages.calls[0]["messages"][0]["content"]


def _budget(calls: int):
    """Настройки с крошечным дневным лимитом вызовов."""
    from gtm.settings import Settings

    return Settings(daily_api_call_limit=calls, daily_api_budget_rub=1000.0)


def test_budget_stops_the_stage_and_keeps_what_was_written(
    session, config, company_factory, monkeypatch
):
    # Лимита хватает ровно на один вызов: второй обязан остановить стадию.
    monkeypatch.setattr("gtm.spend.get_settings", lambda: _budget(1))
    for inn, score in (("7701234567", 0.9), ("7702345678", 0.4)):
        company_factory(inn)
        _enriched(session, inn=inn, score=score)
    sdk = _FakeSdk(_letter_json())
    client = AnthropicClient("test-key", model="claude-opus-5", max_tokens=1200, client=sdk)

    result = run_generate(session, config, run_id="run-12", client=client)

    assert (result.count_in, result.count_out) == (2, 1)
    assert result.details["stopped"] == "budget_exceeded"
    assert result.details["not_generated"] == 1
    # За первое письмо уже заплачено — оно обязано остаться.
    assert session.scalars(select(Outreach)).one().status == "draft"
    assert len(sdk.messages.calls) == 1


def test_failed_call_is_logged_as_spend(session, config):
    class _Broken:
        def create(self, **kwargs: Any):
            raise RuntimeError("сеть недоступна")

    class _BrokenSdk:
        messages = _Broken()

    guard_owner = AnthropicClient(
        "test-key", model="claude-opus-5", max_tokens=1200, client=_BrokenSdk()
    )
    from gtm.generate import LLMError
    from gtm.spend import SpendGuard

    guard_owner.guard = SpendGuard(session, stage="generate")
    with pytest.raises(LLMError):
        guard_owner.complete("промпт")

    spend = session.scalars(select(ApiSpend)).one()
    assert spend.ok is False
    assert spend.cost_rub == 0.0


def test_get_client_falls_back_to_template_without_a_key():
    from gtm.generate import get_client
    from gtm.settings import Settings

    assert isinstance(get_client(Settings(anthropic_api_key=None)), TemplateClient)


def test_window_is_not_a_generate_concern(session, config, company_factory):
    """Стадия работает по статусу, а не по окну: окно закрывает фильтр.

    Проверяем явно, чтобы правка стадии не завела второй источник правды.
    """
    company_factory("7701234567")
    expectation = _enriched(session)
    expectation.window_closes_at = date.today() - timedelta(days=1)
    session.flush()

    result = run_generate(session, config, run_id="run-13", client=TemplateClient())

    assert result.count_out == 1
