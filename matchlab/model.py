from collections import Counter
from datetime import date, timedelta
import math

import numpy as np
from scipy.optimize import minimize
from scipy.stats import poisson, skellam

MODEL_VERSION = "poisson-v1"
WINDOW_DAYS = 3 * 365
MIN_TEAM_MATCHES = 5
MIN_TRAIN_MATCHES = 100


def training_window(rows: list[dict], before: date) -> list[dict]:
    since = (before - timedelta(days=WINDOW_DAYS)).isoformat()
    return [row for row in rows if since <= row["date"] < before.isoformat()]


class GoalModel:
    """Regularized independent Poisson attack/defence model.

    Only completed scores strictly before `before` are consumed. Recency half-life
    is 365 days. Team effects have a zero-centred L2 prior; no tuning on test data.
    """

    def __init__(self, rows: list[dict], before: date):
        self.rows = training_window(rows, before)
        if len(self.rows) < MIN_TRAIN_MATCHES:
            raise ValueError(f"Нужно не менее {MIN_TRAIN_MATCHES} матчей до выбранной даты.")
        self.before = before
        self.counts = Counter(team for row in self.rows for team in (row["home"], row["away"]))
        self.teams = sorted(self.counts)
        self.index = {team: i for i, team in enumerate(self.teams)}
        self.train_until = max(row["date"] for row in self.rows)
        hi = np.array([self.index[row["home"]] for row in self.rows])
        ai = np.array([self.index[row["away"]] for row in self.rows])
        yh = np.array([row["home_goals"] for row in self.rows], dtype=float)
        ya = np.array([row["away_goals"] for row in self.rows], dtype=float)
        ages = np.array([(before - date.fromisoformat(row["date"])).days for row in self.rows])
        weights = np.exp(-math.log(2) * ages / 365)
        n = len(self.teams)
        penalty = 4.0

        def objective(x):
            attack, defence = x[2:2 + n], x[2 + n:]
            lh = x[0] + x[1] + attack[hi] - defence[ai]
            la = x[0] + attack[ai] - defence[hi]
            eh, ea = np.exp(lh), np.exp(la)
            rh, ra = weights * (eh - yh), weights * (ea - ya)
            loss = np.sum(weights * (eh - yh * lh + ea - ya * la))
            loss += penalty * (attack @ attack + defence @ defence) + 2 * (x[1] - 0.2) ** 2
            grad = np.zeros_like(x)
            grad[0], grad[1] = np.sum(rh + ra), np.sum(rh) + 4 * (x[1] - 0.2)
            np.add.at(grad[2:2 + n], hi, rh)
            np.add.at(grad[2:2 + n], ai, ra)
            np.add.at(grad[2 + n:], ai, -rh)
            np.add.at(grad[2 + n:], hi, -ra)
            grad[2:] += 2 * penalty * x[2:]
            return loss, grad

        initial = np.zeros(2 + 2 * n)
        initial[0] = math.log(max(np.average(ya, weights=weights), 0.1))
        initial[1] = 0.2
        fit = minimize(objective, initial, jac=True, method="L-BFGS-B",
                       bounds=[(-1.5, 1.5), (-0.5, 0.8)] + [(-1.5, 1.5)] * (2 * n),
                       options={"maxiter": 300, "ftol": 1e-10})
        if not fit.success or not np.all(np.isfinite(fit.x)):
            raise ValueError("Модель не сошлась; прогноз не сформирован.")
        self.parameters = fit.x
        outcomes = Counter("home" if r["home_goals"] > r["away_goals"] else "away" if r["home_goals"] < r["away_goals"] else "draw" for r in self.rows)
        self.baseline = {key: (outcomes[key] + 1) / (len(self.rows) + 3) for key in ("home", "draw", "away")}

    def eligible(self, home: str, away: str) -> bool:
        return home != away and min(self.counts[home], self.counts[away]) >= MIN_TEAM_MATCHES

    def predict(self, home: str, away: str) -> dict:
        if home == away:
            raise ValueError("Выберите две разные команды.")
        if not self.eligible(home, away):
            raise ValueError(f"Для каждой команды нужно хотя бы {MIN_TEAM_MATCHES} предыдущих матчей АПЛ в окне обучения.")
        n = len(self.teams)
        hi, ai = self.index[home], self.index[away]
        x = self.parameters
        lh = float(np.exp(x[0] + x[1] + x[2 + hi] - x[2 + n + ai]))
        la = float(np.exp(x[0] + x[2 + ai] - x[2 + n + hi]))
        probabilities = np.array([1 - skellam.cdf(0, lh, la), skellam.pmf(0, lh, la), skellam.cdf(-1, lh, la)])
        if not np.all(np.isfinite(probabilities)) or probabilities.sum() <= 0:
            raise ValueError("Не удалось рассчитать вероятности.")
        probabilities = np.maximum(probabilities, 0)
        probabilities /= probabilities.sum()
        limit = min(80, max(10, int(poisson.ppf(0.9999, max(lh, la)))))
        joint = np.outer(poisson.pmf(np.arange(limit + 1), lh), poisson.pmf(np.arange(limit + 1), la))
        scores = []
        for index in np.argsort(joint, axis=None)[-5:][::-1]:
            h, a = np.unravel_index(index, joint.shape)
            scores.append(dict(home=int(h), away=int(a), probability=float(joint[h, a])))
        return {
            "probabilities": dict(zip(("home", "draw", "away"), probabilities.tolist())),
            "expected_goals": {"home": lh, "away": la},
            "scorelines": scores,
            "training": {"matches": len(self.rows), "home_matches": self.counts[home], "away_matches": self.counts[away],
                         "first_match": min(row["date"] for row in self.rows), "last_match": self.train_until,
                         "before": self.before.isoformat(), "half_life_days": 365},
            "model_version": MODEL_VERSION,
        }
