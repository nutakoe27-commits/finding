from contextlib import asynccontextmanager
from collections import OrderedDict
from datetime import date
import json
import math
from pathlib import Path
from threading import RLock
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from .config import Settings
from .model import GoalModel
from . import qwen
from .storage import Repository, utc_now

STATIC = Path(__file__).parent / "static"


class ForecastRequest(BaseModel):
    home: str = Field(min_length=1, max_length=100)
    away: str = Field(min_length=1, max_length=100)
    as_of: date = Field(default_factory=date.today)
    odds: dict[str, float] | None = None

    @model_validator(mode="after")
    def validate_request(self):
        if self.home == self.away:
            raise ValueError("Выберите разные команды.")
        if self.odds is not None:
            if set(self.odds) != {"home", "draw", "away"}:
                raise ValueError("Нужны три коэффициента: home, draw, away.")
            if any(not math.isfinite(v) or not 1 < v <= 1000 for v in self.odds.values()):
                raise ValueError("Коэффициенты должны быть конечными числами больше 1 и не больше 1000.")
            if self.as_of != date.today():
                raise ValueError("Ручные текущие коэффициенты доступны только для сегодняшнего расчёта; история снимков не подключена.")
        return self


class ExplainRequest(BaseModel):
    forecast_id: str = Field(min_length=1, max_length=64)
    notes: str = Field(default="", max_length=6000)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    repo = Repository(settings.database)

    @asynccontextmanager
    async def lifespan(app):
        repo.initialize()
        yield

    app = FastAPI(title="Matchlab", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    models = OrderedDict()
    model_lock = RLock()

    def model_for(before: date) -> tuple[GoalModel, str]:
        rows, revision = repo.snapshot(before)
        key = (before, revision)
        with model_lock:
            if key not in models:
                models[key] = GoalModel(rows, before)
                if len(models) > 16:
                    models.popitem(last=False)
            models.move_to_end(key)
            return models[key], revision

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    def health():
        status = repo.status()
        ready = status["matches"] >= 100
        return JSONResponse({"status": "ok" if ready else "needs_data", "matches": status["matches"]}, status_code=200 if ready else 503)

    @app.get("/api/status")
    def status():
        return {"data": repo.status(), "qwen": {"configured": settings.qwen_configured},
                "live_fixtures": False, "live_odds": False}

    @app.get("/api/teams")
    def teams(as_of: date | None = None):
        before = min(as_of or date.today(), date.today())
        try:
            model, _ = model_for(before)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        # Show teams from the latest loaded season first; older clubs remain
        # available only through the historical date selector.
        rows = repo.matches(before=before)
        latest_season = rows[-1]["season"]
        active = {team for row in rows if row["season"] == latest_season for team in (row["home"], row["away"])}
        return {"teams": [{"name": team, "matches": model.counts[team]} for team in sorted(active) if model.counts[team] >= 5],
                "data_season": latest_season, "before": before.isoformat()}

    @app.post("/api/forecast")
    def forecast(request: ForecastRequest):
        before = min(request.as_of, date.today())
        try:
            model, revision = model_for(before)
            prediction = model.predict(request.home, request.away)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        payload = {"id": uuid4().hex, "generated_at": utc_now(), "home": request.home, "away": request.away,
                   "as_of": request.as_of.isoformat(), "historical_scenario": request.as_of < date.today(),
                   "fixture_verified": False, "dataset_revision": revision, **prediction, "market": None}
        if request.odds:
            total = sum(1 / v for v in request.odds.values())
            payload["market"] = {
                "source": "manual input; not verified with bookmaker", "observed_at": payload["generated_at"],
                "odds": request.odds, "overround": total - 1,
                "probabilities": {k: (1 / v) / total for k, v in request.odds.items()},
                "model_ev": {k: prediction["probabilities"][k] * v - 1 for k, v in request.odds.items()},
                "profitability_verified": False,
            }
        repo.save_forecast(payload)
        return payload

    @app.get("/api/forecasts")
    def forecasts():
        return {"forecasts": repo.recent_forecasts()}

    @app.get("/api/backtest")
    def backtest():
        if not settings.report.exists():
            return {"available": False, "reason": "Историческая проверка ещё не выполнена."}
        try:
            report = json.loads(settings.report.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise HTTPException(503, "Не удалось прочитать отчёт проверки.") from exc
        if report.get("dataset_revision") != repo.revision():
            return {"available": False, "reason": "Данные обновлены. Историческую проверку нужно повторить."}
        return {"available": True, "report": report}

    @app.post("/api/explain")
    def explain(request: ExplainRequest):
        if not settings.qwen_configured:
            raise HTTPException(503, "Сервер Qwen ещё не подключён.")
        saved = repo.forecast(request.forecast_id)
        if saved is None:
            raise HTTPException(404, "Прогноз не найден.")
        try:
            text = qwen.explain(settings, saved, request.notes)
        except ValueError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"forecast_id": saved["id"], "text": text, "notes_verified": False}

    return app


app = create_app()
