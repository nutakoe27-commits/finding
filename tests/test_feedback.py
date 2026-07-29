"""Тесты обратной связи и калибровки.

Главное, что здесь проверяется: исход касания доводится до статуса ожидания
и до стоп-листа, воронка не падает на пустых данных, а калибровка не двигает
веса там, где статистики мало, и не двигает их слишком сильно там, где много.
Порог и ограничение сдвига — это защита от переобучения на трёх письмах,
и ломать их незаметно нельзя.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gtm.config import Config
from gtm.feedback import funnel, import_outcomes_csv, recalibrate, record_outcome
from gtm.storage import repo
from gtm.storage.models import (
    Expectation,
    ExpectationKind,
    ExpectationStatus,
    Outcome,
    Outreach,
    OutreachStatus,
    Suppression,
    SuppressionReason,
)

INN = "7701234567"
ANNIVERSARY = ExpectationKind.EVENT_ANNIVERSARY.value
GROWTH = ExpectationKind.GROWTH.value


def _outreach(
    session: Session,
    seq: int,
    *,
    kind: str = ANNIVERSARY,
    inn: str = INN,
    status: str = ExpectationStatus.DELIVERED.value,
) -> Outreach:
    """Готовое к отправке касание. `seq` разводит ожидания по месяцам:
    ключ дедупликации — inn|kind|месяц, иначе второе ожидание не создастся."""
    expectation, _ = repo.upsert_expectation(
        session,
        inn=inn,
        kind=kind,
        expected_at=date(2027 + seq // 12, seq % 12 + 1, 15),
        window_opens_at=date(2026, 7, 1),
        window_closes_at=date(2026, 9, 1),
        confidence=0.6,
    )
    expectation.status = status
    session.flush()
    return repo.create_outreach(
        session,
        expectation_id=expectation.id,
        contact_id=None,
        subject="Тема",
        body="Текст письма",
        generated_by="test",
    )


def _with_outcomes(session: Session, kind: str, outcomes: list[str], *, start: int = 0) -> None:
    """Пачка касаний с заданными исходами — вход для калибровки и воронки."""
    for offset, outcome in enumerate(outcomes):
        row = _outreach(session, start + offset, kind=kind)
        record_outcome(session, row.id, outcome)


@pytest.fixture()
def tuned(config: Config) -> Config:
    """Порог в 25 исходов принципиален в бою, но в тесте важна не цифра,
    а то, что порог соблюдается. Снижаем, чтобы не плодить сотни строк."""
    copy = config.model_copy(deep=True)
    copy.signals.calibration.min_outcomes_per_kind = 4
    return copy


# --------------------------------------------------------------- запись исхода


def test_booked_closes_expectation_as_won(session: Session, company_factory) -> None:
    company_factory(INN)
    row = _outreach(session, 0)

    record_outcome(session, row.id, Outcome.BOOKED.value)

    assert session.get(Expectation, row.expectation_id).status == ExpectationStatus.WON.value


@pytest.mark.parametrize("outcome", [Outcome.REPLY_NEGATIVE.value, Outcome.LOST.value])
def test_refusal_closes_expectation_as_lost(
    session: Session, company_factory, outcome: str
) -> None:
    company_factory(INN)
    row = _outreach(session, 0)

    record_outcome(session, row.id, outcome)

    assert session.get(Expectation, row.expectation_id).status == ExpectationStatus.LOST.value


@pytest.mark.parametrize(
    "outcome",
    [Outcome.NO_REPLY.value, Outcome.REPLY_POSITIVE.value, Outcome.MEETING.value],
)
def test_intermediate_outcome_keeps_expectation_status(
    session: Session, company_factory, outcome: str
) -> None:
    """Встреча и показ зала — ещё не результат: закрывать ожидание рано,
    а молчание не повод считать его проигранным."""
    company_factory(INN)
    row = _outreach(session, 0)

    record_outcome(session, row.id, outcome)

    expectation = session.get(Expectation, row.expectation_id)
    assert expectation.status == ExpectationStatus.DELIVERED.value


def test_outcome_marks_letter_as_sent(session: Session, company_factory) -> None:
    """Отправляет письмо менеджер со своего ящика — записанный исход
    единственное доказательство, что оно ушло. Иначе воронка покажет ноль
    отправленных при полном списке броней."""
    company_factory(INN)
    row = _outreach(session, 0)
    assert row.sent_at is None

    record_outcome(session, row.id, Outcome.NO_REPLY.value)

    assert row.sent_at is not None
    assert row.status == OutreachStatus.SENT.value


def test_outcome_does_not_overwrite_known_send_time(session: Session, company_factory) -> None:
    company_factory(INN)
    row = _outreach(session, 0)
    record_outcome(session, row.id, Outcome.NO_REPLY.value)
    first = row.sent_at

    record_outcome(session, row.id, Outcome.REPLY_POSITIVE.value)

    assert row.sent_at == first
    assert row.status == OutreachStatus.REPLIED.value


def test_unknown_outcome_rejected_and_nothing_written(
    session: Session, company_factory
) -> None:
    company_factory(INN)
    row = _outreach(session, 0)

    with pytest.raises(ValueError, match="booked"):
        record_outcome(session, row.id, "забронировали")

    assert row.outcome == Outcome.PENDING.value
    assert row.sent_at is None


def test_unknown_outreach_raises(session: Session) -> None:
    with pytest.raises(KeyError):
        record_outcome(session, 4242, Outcome.NO_REPLY.value)


def test_note_is_appended_not_replaced(session: Session, company_factory) -> None:
    company_factory(INN)
    row = _outreach(session, 0)

    record_outcome(session, row.id, Outcome.MEETING.value, note="встреча 3 августа")
    record_outcome(session, row.id, Outcome.LOST.value, note="выбрали Крокус")

    assert "встреча 3 августа" in row.notes
    assert "выбрали Крокус" in row.notes


# ------------------------------------------------------------------ стоп-лист


def test_suppress_adds_company_to_stop_list(session: Session, company_factory) -> None:
    company_factory(INN)
    row = _outreach(session, 0)

    record_outcome(
        session, row.id, Outcome.REPLY_NEGATIVE.value, note="просили не писать", suppress=True
    )

    assert repo.is_suppressed(session, inn=INN)
    entry = session.scalars(select(Suppression)).one()
    assert entry.reason == SuppressionReason.OPTED_OUT.value
    assert entry.note == "просили не писать"


def test_suppress_twice_does_not_duplicate(session: Session, company_factory) -> None:
    company_factory(INN)
    first = _outreach(session, 0)
    second = _outreach(session, 1)

    record_outcome(session, first.id, Outcome.REPLY_NEGATIVE.value, suppress=True)
    record_outcome(session, second.id, Outcome.NO_REPLY.value, suppress=True)

    assert session.scalar(select(func.count()).select_from(Suppression)) == 1


def test_no_suppression_without_flag(session: Session, company_factory) -> None:
    """Отказ по этому мероприятию не значит «не писать больше никогда»:
    в стоп-лист компания уходит только по явной просьбе."""
    company_factory(INN)
    row = _outreach(session, 0)

    record_outcome(session, row.id, Outcome.REPLY_NEGATIVE.value)

    assert not repo.is_suppressed(session, inn=INN)


# --------------------------------------------------------------------- импорт


def test_import_csv_applies_rows_and_counts_failures(
    session: Session, company_factory, tmp_path: Path
) -> None:
    company_factory(INN)
    good = _outreach(session, 0)
    typo = _outreach(session, 1)
    path = tmp_path / "outcomes.csv"
    path.write_text(
        "outreach_id,outcome,note\n"
        f"{good.id},booked,корпоратив 12 декабря\n"
        f"{typo.id},забронировали,\n"
        "9999,booked,\n"
        ",no_reply,строка без номера\n",
        encoding="utf-8",
    )

    stats = import_outcomes_csv(session, path)

    assert stats == {"rows": 4, "applied": 1, "skipped": 1, "not_found": 1, "bad_outcome": 1}
    assert good.outcome == Outcome.BOOKED.value
    assert good.notes == "корпоратив 12 декабря"
    assert session.get(Expectation, good.expectation_id).status == ExpectationStatus.WON.value
    # Строка с опечаткой не должна была примениться частично.
    assert typo.outcome == Outcome.PENDING.value


def test_import_csv_reads_excel_dialect(
    session: Session, company_factory, tmp_path: Path
) -> None:
    """Таблицу заполняет менеджер в Excel: точка с запятой и cp1251."""
    company_factory(INN)
    row = _outreach(session, 0)
    path = tmp_path / "outcomes-excel.csv"
    path.write_text(
        f"outreach_id;outcome;note\n{row.id};viewing;приехали смотреть зал\n",
        encoding="cp1251",
    )

    stats = import_outcomes_csv(session, path)

    assert stats["applied"] == 1
    assert row.outcome == Outcome.VIEWING.value
    assert row.notes == "приехали смотреть зал"


# -------------------------------------------------------------------- воронка


def test_funnel_counts_steps_and_conversions(session: Session, company_factory) -> None:
    company_factory(INN)
    _with_outcomes(
        session,
        ANNIVERSARY,
        [Outcome.NO_REPLY.value] * 4
        + [Outcome.REPLY_NEGATIVE.value] * 2
        + [
            Outcome.REPLY_POSITIVE.value,
            Outcome.MEETING.value,
            Outcome.VIEWING.value,
            Outcome.BOOKED.value,
        ],
    )
    _outreach(session, 20)  # черновик — ещё не отправлен, в воронку не входит

    data = funnel(session)

    # Исход — самая дальняя точка: бронь считается и ответом, и показом зала.
    assert data["sent"] == 10
    assert data["replies"] == 6
    assert data["positive"] == 4
    assert data["viewings"] == 2
    assert data["booked"] == 1
    assert data["reply_rate"] == pytest.approx(0.6)
    assert data["positive_rate"] == pytest.approx(4 / 6, abs=1e-4)
    assert data["viewing_rate"] == pytest.approx(0.5)
    assert data["booking_rate"] == pytest.approx(0.5)
    assert data["sent_to_booked"] == pytest.approx(0.1)


def test_funnel_on_empty_base_returns_zeros(session: Session) -> None:
    """Первые недели воронка пустая — падать на делении на ноль нельзя."""
    data = funnel(session)

    assert data["sent"] == 0
    assert data["reply_rate"] == 0.0
    assert data["positive_rate"] == 0.0
    assert data["booking_rate"] == 0.0


def test_funnel_without_replies_has_zero_downstream_rates(
    session: Session, company_factory
) -> None:
    company_factory(INN)
    _with_outcomes(session, ANNIVERSARY, [Outcome.NO_REPLY.value] * 3)

    data = funnel(session)

    assert data["sent"] == 3
    assert data["reply_rate"] == 0.0
    # Ни одного ответа — знаменатель следующей ступени нулевой.
    assert data["positive_rate"] == 0.0


def test_funnel_since_cuts_off_old_letters(
    session: Session, company_factory, today: date
) -> None:
    company_factory(INN)
    old = _outreach(session, 0)
    fresh = _outreach(session, 1)
    record_outcome(session, old.id, Outcome.BOOKED.value)
    record_outcome(session, fresh.id, Outcome.NO_REPLY.value)
    old.sent_at = old.sent_at.replace(year=old.sent_at.year - 1)
    session.flush()

    data = funnel(session, since=today - timedelta(days=30))

    assert data["sent"] == 1
    assert data["booked"] == 0


# ------------------------------------------------------------------ калибровка


def test_kind_without_statistics_is_skipped(
    session: Session, company_factory, config: Config
) -> None:
    """Три письма — не статистика. Порог из конфига защищает от того,
    чтобы сигнал выключился по случайности."""
    company_factory(INN)
    _with_outcomes(session, ANNIVERSARY, [Outcome.BOOKED.value] * 3)

    report = recalibrate(session, config)

    assert ANNIVERSARY not in report.per_kind
    assert "3" in report.skipped[ANNIVERSARY]
    assert str(config.signals.calibration.min_outcomes_per_kind) in report.skipped[ANNIVERSARY]
    assert report.yaml_snippet == ""


def test_pending_outreach_does_not_count_as_outcome(
    session: Session, company_factory, tuned: Config
) -> None:
    """Касание без результата — это отсутствие статистики, а не ноль в ней."""
    company_factory(INN)
    _with_outcomes(session, ANNIVERSARY, [Outcome.BOOKED.value] * 3)
    for seq in range(10, 20):
        _outreach(session, seq)  # отправлены, исхода ещё нет

    report = recalibrate(session, tuned)

    assert "исходов 3" in report.skipped[ANNIVERSARY]


def test_unknown_kind_is_reported_not_dropped(
    session: Session, company_factory, tuned: Config
) -> None:
    company_factory(INN)
    _with_outcomes(session, "legacy_kind", [Outcome.BOOKED.value] * 5)

    report = recalibrate(session, tuned)

    assert "legacy_kind" in report.skipped
    assert "signals.yaml" in report.skipped["legacy_kind"]


def test_weight_shift_is_capped_in_both_directions(
    session: Session, company_factory, tuned: Config
) -> None:
    """Сильный тип не должен утроить вес за один пересчёт, слабый —
    обнулиться: иначе система будет писать только по одному сигналу."""
    company_factory(INN)
    _with_outcomes(session, ANNIVERSARY, [Outcome.BOOKED.value] * 4)
    _with_outcomes(session, GROWTH, [Outcome.NO_REPLY.value] * 4, start=10)
    max_shift = tuned.signals.calibration.max_weight_shift

    report = recalibrate(session, tuned)

    strong = report.per_kind[ANNIVERSARY]
    weak = report.per_kind[GROWTH]
    # Конверсия 100% против средней 50% — отношение 2.0, но сдвиг ограничен.
    assert strong["conversion"] == 1.0
    assert strong["proposed_weight"] == pytest.approx(
        strong["current_weight"] * (1 + max_shift), abs=0.01
    )
    assert weak["conversion"] == 0.0
    assert weak["proposed_weight"] == pytest.approx(
        weak["current_weight"] * (1 - max_shift), abs=0.01
    )
    assert weak["proposed_weight"] > 0
    assert strong["applied"] and weak["applied"]


def test_weight_stays_within_absolute_bounds(
    session: Session, company_factory, tuned: Config
) -> None:
    """Ноль выключил бы сигнал молча, вес выше полутора ломает
    сопоставимость приоритетов между типами."""
    tuned.signals.calibration.max_weight_shift = 5.0
    tuned.signals.kinds[ANNIVERSARY].weight = 1.4
    tuned.signals.kinds[GROWTH].weight = 0.06
    company_factory(INN)
    _with_outcomes(session, ANNIVERSARY, [Outcome.BOOKED.value] * 4)
    _with_outcomes(session, GROWTH, [Outcome.NO_REPLY.value] * 4, start=10)

    report = recalibrate(session, tuned)

    assert report.per_kind[ANNIVERSARY]["proposed_weight"] == 1.5
    assert report.per_kind[GROWTH]["proposed_weight"] == 0.05


def test_equal_conversions_leave_weights_alone(
    session: Session, company_factory, tuned: Config
) -> None:
    company_factory(INN)
    _with_outcomes(session, ANNIVERSARY, [Outcome.BOOKED.value, *[Outcome.NO_REPLY.value] * 3])
    _with_outcomes(
        session, GROWTH, [Outcome.BOOKED.value, *[Outcome.NO_REPLY.value] * 3], start=10
    )

    report = recalibrate(session, tuned)

    for row in report.per_kind.values():
        assert row["proposed_weight"] == row["current_weight"]
        assert row["applied"] is False


def test_no_positives_anywhere_is_skipped(
    session: Session, company_factory, tuned: Config
) -> None:
    """Средняя конверсия ноль — относительное отношение не определено,
    и любое предложенное число было бы выдумкой."""
    company_factory(INN)
    _with_outcomes(session, ANNIVERSARY, [Outcome.NO_REPLY.value] * 4)

    report = recalibrate(session, tuned)

    assert report.per_kind == {}
    assert "положительного" in report.skipped[ANNIVERSARY]
    assert report.yaml_snippet == ""


def test_yaml_snippet_parses_back(session: Session, company_factory, tuned: Config) -> None:
    """Фрагмент вставляет человек — он обязан быть валидным YAML
    и совпадать с тем, что показано в отчёте."""
    company_factory(INN)
    _with_outcomes(session, ANNIVERSARY, [Outcome.BOOKED.value] * 4)
    _with_outcomes(session, GROWTH, [Outcome.NO_REPLY.value] * 4, start=10)

    report = recalibrate(session, tuned)
    parsed = yaml.safe_load(report.yaml_snippet)

    assert set(parsed["kinds"]) == {ANNIVERSARY, GROWTH}
    for kind, row in report.per_kind.items():
        assert parsed["kinds"][kind]["weight"] == row["proposed_weight"]


def test_recalibrate_never_touches_config_file(
    session: Session, company_factory, tuned: Config
) -> None:
    """Программная правка YAML затирает комментарии, а они здесь —
    половина ценности конфига."""
    path = Path(__file__).resolve().parent.parent / "config" / "signals.yaml"
    before = path.read_text(encoding="utf-8")
    company_factory(INN)
    _with_outcomes(session, ANNIVERSARY, [Outcome.BOOKED.value] * 4)
    _with_outcomes(session, GROWTH, [Outcome.NO_REPLY.value] * 4, start=10)

    recalibrate(session, tuned)

    assert path.read_text(encoding="utf-8") == before
    assert tuned.signals.kind(ANNIVERSARY).weight == 1.0


def test_outcomes_survive_for_recalibration(session: Session, company_factory) -> None:
    """История касаний — то, ради чего всё хранится: исход остаётся
    привязан к типу сигнала, иначе калибровать будет нечего."""
    company_factory(INN)
    _with_outcomes(session, ANNIVERSARY, [Outcome.BOOKED.value, Outcome.NO_REPLY.value])

    by_kind = repo.outcomes_by_kind(session)

    assert by_kind[ANNIVERSARY] == {Outcome.BOOKED.value: 1, Outcome.NO_REPLY.value: 1}
    assert session.scalar(select(func.count()).select_from(Outreach)) == 2
