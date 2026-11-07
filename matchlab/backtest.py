from collections import defaultdict
from datetime import date
import json
import math
from pathlib import Path

from .model import GoalModel, MODEL_VERSION
from .storage import Repository, utc_now

KEYS = ("home", "draw", "away")


def outcome(row: dict) -> int:
    return 0 if row["home_goals"] > row["away_goals"] else 2 if row["home_goals"] < row["away_goals"] else 1


def metrics(predictions: list[list[float]], actual: list[int]) -> dict:
    if not actual:
        raise ValueError("Нет оценённых матчей; проверка не состоялась.")
    size = len(actual)
    return {
        "matches": size,
        "log_loss": -sum(math.log(max(p[y], 1e-15)) for p, y in zip(predictions, actual)) / size,
        "brier": sum(sum((p[k] - int(k == y)) ** 2 for k in range(3)) for p, y in zip(predictions, actual)) / size,
        "accuracy": sum(max(range(3), key=lambda k: p[k]) == y for p, y in zip(predictions, actual)) / size,
    }


def calibration(predictions: list[list[float]], actual: list[int]) -> list[dict]:
    bins = defaultdict(list)
    for probabilities, truth in zip(predictions, actual):
        for k, p in enumerate(probabilities):
            bins[min(9, int(p * 10))].append((p, int(k == truth)))
    return [dict(lower=b / 10, upper=(b + 1) / 10, count=len(values),
                 predicted=sum(p for p, _ in values) / len(values),
                 observed=sum(y for _, y in values) / len(values))
            for b, values in sorted(bins.items())]


def run_backtest(repo: Repository, seasons: list[str]) -> dict:
    rows, revision = repo.snapshot()
    missing = set(seasons) - {row["season"] for row in rows}
    if missing:
        raise ValueError("Не загружены сезоны: " + ", ".join(sorted(missing)))
    selected = [row for row in rows if row["season"] in seasons]
    months = defaultdict(list)
    for row in selected:
        months[row["date"][:7]].append(row)
    all_predictions, all_baselines, all_actual = [], [], []
    monthly = []
    skipped = 0
    for month, fixtures in sorted(months.items()):
        before = date.fromisoformat(month + "-01")
        # Training is frozen at the beginning of the month. Even other results
        # from the SAME test month cannot enter the fit.
        model = GoalModel(rows, before)
        predictions, baselines, actual = [], [], []
        for row in fixtures:
            if not model.eligible(row["home"], row["away"]):
                skipped += 1
                continue
            result = model.predict(row["home"], row["away"])
            predictions.append([result["probabilities"][k] for k in KEYS])
            baselines.append([model.baseline[k] for k in KEYS])
            actual.append(outcome(row))
        if actual:
            monthly.append({"month": month, "training_last_match": model.train_until,
                            "model": metrics(predictions, actual), "baseline": metrics(baselines, actual)})
            all_predictions.extend(predictions)
            all_baselines.extend(baselines)
            all_actual.extend(actual)
    if not all_actual:
        raise ValueError("Ноль оценённых матчей; недостаточно истории команд.")
    report = {
        "generated_at": utc_now(), "dataset_revision": revision, "model_version": MODEL_VERSION,
        "seasons": seasons, "first_test_date": min(r["date"] for r in selected),
        "last_test_date": max(r["date"] for r in selected),
        "method": "monthly rolling-origin evaluation; rolling 1095-day training window; dates strictly before month start",
        "model": metrics(all_predictions, all_actual), "baseline": metrics(all_baselines, all_actual),
        "skipped_matches": skipped, "selected_matches": len(selected),
        "skip_reason": "fewer than 5 prior EPL matches for at least one team",
        "monthly": monthly, "calibration": calibration(all_predictions, all_actual),
        "market_comparison": {"available": False, "reason": "Нет исторических снимков коэффициентов с подтверждённым временем. Коэффициенты закрытия не подставляются."},
        "profitability_verified": False,
    }
    return report


def save_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temp.replace(path)
