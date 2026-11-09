from contextlib import asynccontextmanager
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
import json
import math
import secrets
from pathlib import Path
from threading import RLock
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from .config import Settings
from .model import GoalModel
from . import qwen, archive
from .bookmakers import FORMAT as BOOKMAKER_FORMAT, instant, model_teams
from .storage import Repository, utc_now
from .winline_browser import statistics_id_from_url

STATIC = Path(__file__).parent / "static"


class ForecastRequest(BaseModel):
    home: str = Field(min_length=1, max_length=100)
    away: str = Field(min_length=1, max_length=100)
    as_of: date = Field(default_factory=date.today)
    odds: dict[str, float] | None = None
    quote_id: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_request(self):
        if self.home == self.away:
            raise ValueError("Выберите разные команды.")
        if self.quote_id and self.odds is not None:
            raise ValueError("Выберите снимок букмекера или ручные коэффициенты.")
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


class EventExplainRequest(BaseModel):
    content_version: str | None = Field(default=None,pattern=r"^[a-f0-9]{32}$")
    market_key: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    outcome_key: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    observed_at: str | None = Field(default=None, max_length=50)
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def selection_requires_market(self):
        if self.outcome_key and not self.market_key:
            raise ValueError("Сначала выберите рынок.")
        return self


def public_ai_error(error: ValueError) -> str:
    if "вовремя" in str(error):
        return "Разбор не успел завершиться. Попробуйте ещё раз немного позже."
    if "лимита токенов" in str(error):
        return "Разбор не удалось завершить. Выберите конкретный рынок и повторите запрос."
    return "Не удалось получить разбор ИИ. Попробуйте позже или проверьте подключение в состоянии сервиса."


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    repo = Repository(settings.database)

    @asynccontextmanager
    async def lifespan(app):
        repo.initialize()
        yield

    app = FastAPI(title="prognozich ai", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    models = OrderedDict()
    model_lock = RLock()
    analysis_lock = RLock()

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
        archive_state=archive.overview(repo)
        return {"data": repo.status(), "qwen": {"configured": settings.qwen_configured}, "ai": {"configured": settings.qwen_configured},
                "bookmakers": repo.bookmaker_status(),
                "archive":archive_state, "live_fixtures": archive_state["available"] and not archive_state["source_stale"], "live_odds": archive_state["available"] and not archive_state["source_stale"]}

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
        quote = repo.bookmaker_quote(request.quote_id) if request.quote_id else None
        if request.quote_id:
            if quote is None:
                raise HTTPException(404, "Снимок коэффициентов не найден.")
            canonical = model_teams(quote)
            if canonical != (request.home, request.away):
                raise HTTPException(422, "Команды или лига не соответствуют выбранному снимку АПЛ.")
            if instant(quote["kickoff_at"]) <= datetime.now(timezone.utc):
                raise HTTPException(409, "Матч уже начался. Предматчевый расчёт по этому снимку закрыт.")
            before = instant(quote["observed_at"]).date()
        try:
            model, revision = model_for(before)
            prediction = model.predict(request.home, request.away)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        payload = {"id": uuid4().hex, "generated_at": utc_now(), "home": request.home, "away": request.away,
                   "as_of": before.isoformat() if quote else request.as_of.isoformat(), "historical_scenario": before < date.today(),
                   "fixture_verified": bool(quote), "dataset_revision": revision, **prediction, "market": None}
        prices = quote["odds"] if quote else request.odds
        if prices:
            total = sum(1 / v for v in prices.values())
            payload["market"] = {
                "source": quote["source"] if quote else "manual input; not verified with bookmaker",
                "observed_at": quote["observed_at"] if quote else payload["generated_at"],
                "odds": prices, "overround": total - 1,
                "probabilities": {k: (1 / v) / total for k, v in prices.items()},
                "model_ev": {k: prediction["probabilities"][k] * v - 1 for k, v in prices.items()},
                "profitability_verified": False,
            }
        if quote:
            payload.update(quote_id=quote["id"], kickoff_at=quote["kickoff_at"], home_label=quote["home"], away_label=quote["away"])
            payload["market"].update(parser_version=quote["parser_version"], provenance="captured public DOM", is_stale=(datetime.now(timezone.utc) - instant(quote["observed_at"])).total_seconds() > 300)
        repo.save_forecast(payload)
        return payload

    @app.get("/api/bookmakers")
    def bookmakers(include_started: bool = False, source: str | None = None):
        if source not in (None, "betboom", "winline"):
            raise HTTPException(422, "Неизвестный букмекер.")
        current = datetime.now(timezone.utc)
        result = []
        for quote in repo.bookmaker_quotes(include_started=include_started, source=source):
            canonical = model_teams(quote)
            reason = "Модель пока поддерживает только АПЛ с известными командами."
            eligible = False
            if canonical:
                try:
                    model, _ = model_for(instant(quote["observed_at"]).date())
                    eligible = model.eligible(*canonical)
                    reason = "" if eligible else "Недостаточно истории этой команды в АПЛ."
                except ValueError as exc:
                    reason = str(exc)
            started = instant(quote["kickoff_at"]) <= current
            if started:
                eligible, reason = False, "Матч уже начался."
            age = max(0, int((current - instant(quote["observed_at"])).total_seconds()))
            result.append({**quote, "model_teams": list(canonical) if canonical else None,
                           "prediction_available": eligible, "unavailable_reason": reason,
                           "age_seconds": age, "is_stale": age > 300, "started": started})
        return {"quotes": result, "status": repo.bookmaker_status(), "freshness_seconds": 300,
                "coverage": "captured visible football pre-match 1X2 cards"}

    def require_worker(request: Request):
        if not settings.bookmaker_ingest_token:
            raise HTTPException(503, "Серверный сборщик ещё не настроен.")
        if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
            raise HTTPException(403, "Импорт сборщика доступен только локально на VPS.")
        provided = request.headers.get("X-Matchlab-Collector-Key", "")
        if not secrets.compare_digest(provided.encode(), settings.bookmaker_ingest_token.encode()):
            raise HTTPException(401, "Неверный ключ сборщика.")

    async def worker_body(request: Request, limit: int) -> dict:
        require_worker(request)
        parts, length = [], 0
        async for part in request.stream():
            length += len(part)
            if length > limit:
                raise HTTPException(413, "Пакет сборщика слишком большой.")
            parts.append(part)
        try:
            body = json.loads(b"".join(parts))
        except ValueError as exc:
            raise HTTPException(400, "Нужен JSON-объект.") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "Нужен JSON-объект.")
        return body

    @app.post('/api/bookmakers/archive/ingest')
    async def archive_ingest(request:Request):
        body=await worker_body(request,2*1024*1024)
        try:return archive.ingest(repo,body)
        except (ValueError,KeyError,TypeError,AttributeError) as exc:
            raise HTTPException(422,'Некорректный пакет архива') from exc

    @app.get('/api/bookmakers/archive/watch')
    def archive_watch_queue(request:Request):
        require_worker(request)
        return {'events':archive.watches(repo),'format':'matchlab.archive.v1'}

    @app.get('/api/archive/status')
    def archive_status():
        return {'format':'matchlab.archive.v1',**archive.overview(repo)}

    @app.get('/api/archive/results')
    def archive_results(search:str='',sport:int|None=None,limit:int=50,offset:int=0):
        if len(search)>200 or not 1<=limit<=100 or offset<0:raise HTTPException(422,'Некорректная страница архива')
        return archive.results(repo,search,sport,limit,offset)

    @app.get('/api/archive/events/{event_id}/markets')
    def stream_detail(event_id:str):
        document=archive.document(repo,event_id)
        if not document:raise HTTPException(404,'Событие отсутствует в последнем потоке')
        return document

    @app.get('/api/archive/events/{event_id}/statistics')
    def stream_statistics(event_id:str):
        native=archive.document(repo,event_id);legacy=repo.event_markets(event_id)
        linked=bool(native and legacy and archive.key(native['home'] or '')==archive.key(legacy.get('home') or '')
                    and archive.key(native['away'] or '')==archive.key(legacy.get('away') or '')
                    and archive.key(native['sport_label'])==archive.key(legacy.get('sport_label') or ''))
        if linked:
            linked=bool(native.get('kickoff_at') and legacy.get('kickoff_at') and
                        abs((instant(native['kickoff_at'])-instant(legacy['kickoff_at'])).total_seconds())<=120)
        snapshot=repo.statistics(event_id) if linked else None
        if snapshot and snapshot['statistics_id']!=statistics_id_from_url(legacy.get('statistics_url') or ''):snapshot=None
        return {'statistics':snapshot,'link_status':legacy.get('statistics_link_status','missing') if linked else 'missing',
                'statistics_url':legacy.get('statistics_url') if linked else None,'source_blocked_until':repo.statistics_blocked_until()}

    @app.get('/api/archive/events/{event_id}/history')
    def archive_history(event_id:str,source:str='stream'):
        doc=archive.document(repo,event_id) if source=='stream' else repo.event_markets(event_id)
        if not doc:raise HTTPException(404,'Матч не найден')
        return archive.history(repo,doc)

    @app.post('/api/archive/events/{event_id}/watch')
    def request_archive_watch(event_id:str,request:Request):
        if request.headers.get('content-type','').split(';')[0].strip().lower()!='application/json' or request.headers.get('sec-fetch-site','') in ('cross-site','same-site'):
            raise HTTPException(403,'Обновление доступно со страницы сервиса')
        try:return archive.watch(repo,event_id)
        except LookupError as exc:raise HTTPException(404,str(exc)) from exc

    @app.get('/api/archive/events/{event_id}/analysis')
    def stream_analysis(event_id:str):
        return {'prediction_available':False,'unavailable_reason':'ИИ использует наблюдаемые рынки и историю участников. Независимая вероятностная модель для этого потока не подключена.'}

    @app.post("/api/bookmakers/ingest")
    async def worker_ingest(request: Request):
        body = await worker_body(request, 2 * 1024 * 1024)
        if body.get("format") != BOOKMAKER_FORMAT:
            raise HTTPException(400, "Ожидаются нормализованные публичные снимки.")
        try:
            return repo.import_bookmakers(body)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise HTTPException(422, "Некорректные снимки. Проверьте формат, коэффициенты и часы сборщика.") from exc

    @app.post("/api/bookmakers/worker-status")
    async def worker_status(request: Request):
        body = await worker_body(request, 32768)
        try:
            return repo.save_worker_status(body)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/bookmakers/events/ingest")
    async def event_market_ingest(request: Request):
        body = await worker_body(request, 2 * 1024 * 1024)
        try:
            return repo.import_event_markets(body)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise HTTPException(422, "Некорректный документ рынков события.") from exc

    @app.get('/api/bookmakers/statistics/queue')
    def statistics_queue(request: Request,limit: int=10):
        require_worker(request)
        if not 1<=limit<=50: raise HTTPException(422,'Некорректный размер очереди.')
        return repo.statistics_queue(limit)

    @app.post('/api/bookmakers/statistics/ingest')
    async def statistics_ingest(request: Request):
        body=await worker_body(request,512*1024)
        try: return repo.import_statistics(body)
        except (ValueError,TypeError,KeyError,AttributeError) as error:
            raise HTTPException(422,'Некорректный снимок статистики события.') from error

    @app.post('/api/bookmakers/refresh/claim')
    async def claim_refresh(request: Request):
        await worker_body(request,1024)
        return {'job':repo.claim_event_refresh()}

    @app.post('/api/bookmakers/refresh/finish')
    async def finish_refresh(request: Request):
        body=await worker_body(request,4096)
        try:
            done=repo.finish_event_refresh(body.get('job_id',''),body.get('lease_id',''),body.get('market_state',''),body.get('statistics_state',''))
        except ValueError as error:
            raise HTTPException(422,str(error)) from error
        if not done: raise HTTPException(409,'Задание устарело или уже завершено.')
        return {'finished':True}

    @app.post('/api/bookmakers/events/{event_id}/refresh',status_code=202)
    def request_refresh(event_id: str, request: Request):
        if not event_id.isdigit() or len(event_id)>20: raise HTTPException(422,'Некорректный ID события.')
        if request.headers.get('content-type','').split(';')[0].strip().lower()!='application/json' or request.headers.get('sec-fetch-site','') in ('cross-site','same-site'):
            raise HTTPException(403,'Обновление доступно со страницы сервиса.')
        if not settings.bookmaker_ingest_token: raise HTTPException(503,'Сборщик ещё не настроен.')
        try: return repo.request_event_refresh(event_id)
        except LookupError as error: raise HTTPException(404,str(error)) from error
        except ValueError as error: raise HTTPException(429,str(error)) from error

    @app.get('/api/bookmakers/events/{event_id}/refresh/{job_id}')
    def refresh_status(event_id: str,job_id: str):
        if not event_id.isdigit() or len(event_id)>20 or len(job_id)!=32 or any(char not in '0123456789abcdef' for char in job_id):
            raise HTTPException(422,'Некорректное задание обновления.')
        result=repo.event_refresh(event_id,job_id)
        if not result: raise HTTPException(404,'Задание не найдено или заменено более новым.')
        return result

    @app.get('/api/bookmakers/events/{event_id}/statistics')
    def statistics_detail(event_id: str):
        event=repo.event_markets(event_id)
        if not event: raise HTTPException(404,'Событие не найдено.')
        result=repo.statistics(event_id)
        if result and result['statistics_id']!=statistics_id_from_url(event.get('statistics_url') or ''):
            result=None
        return {'statistics':result,'link_status':event.get('statistics_link_status','missing'),'statistics_url':event.get('statistics_url'),
                'source_blocked_until':repo.statistics_blocked_until()}

    @app.get("/api/bookmakers/events")
    def market_events(include_started: bool = False, limit: int = 100, offset: int = 0,
                      search: str = "", competition: str = "", phase: str = "", day: date | None = None,
                      ids: str | None = None, sort: str = "kickoff", sport: str = ""):
        from .sports import ordinary_sport
        event_ids = ids.split(",") if ids is not None else None
        if (not 1 <= limit <= 200 or offset < 0 or len(search) > 200 or len(competition) > 200 or (sport and not ordinary_sport(sport))
                or phase not in ("", "upcoming", "live") or sort not in ("kickoff", "freshness")
                or (event_ids is not None and (len(event_ids) > 100 or any(not item.isdigit() or len(item) > 20 for item in event_ids)))):
            raise HTTPException(422, "Некорректная страница списка событий.")
        if archive.overview(repo)['available']:
            return archive.catalog(repo,include_started=include_started,limit=limit,offset=offset,
                                   search=search.strip(),competition=competition,phase=phase,
                                   day=day.isoformat() if day else None,event_ids=event_ids,sort=sort,sport=sport)
        result = repo.market_events(include_started=include_started, limit=limit, offset=offset,
                                   search=search.strip(), competition=competition, phase=phase,
                                   day=day.isoformat() if day else None, event_ids=event_ids, sort=sort, sport=sport)
        current = datetime.now(timezone.utc)
        for event in result["events"]:
            event["age_seconds"] = max(0, int((current - instant(event["capture_started_at"])).total_seconds()))
            event["is_stale"] = event["age_seconds"] > 180
        return result

    @app.get("/api/bookmakers/events/{event_id}/markets")
    def market_detail(event_id: str):
        if not event_id.isdigit() or len(event_id) > 20:
            raise HTTPException(422, "Некорректный ID события.")
        document = repo.event_markets(event_id)
        if not document:
            raise HTTPException(404, "Рынки этого события ещё не собраны.")
        age = max(0, int((datetime.now(timezone.utc) - instant(document["capture_started_at"])).total_seconds()))
        from .markets import headline_preview
        return {**document, "age_seconds": age, "is_stale": age > 180, "headline": headline_preview(document)}

    @app.get("/api/bookmakers/events/{event_id}/analysis")
    def event_analysis(event_id: str):
        from .markets import main_odds
        if not event_id.isdigit() or len(event_id) > 20:
            raise HTTPException(422, "Некорректный ID события.")
        document = repo.event_markets(event_id)
        if not document:
            raise HTTPException(404, "Матч ещё не собран.")
        quote = repo.event_quote(event_id)
        reason = "Модель пока поддерживает основной исход АПЛ. Рынки этой встречи доступны для просмотра."
        eligible, canonical = False, model_teams(document)
        if document["phase"] in ("live", "finished") or (document["kickoff_at"] and instant(document["kickoff_at"]) <= datetime.now(timezone.utc)):
            reason = "Предматчевый прогноз для начавшейся встречи недоступен."
        elif document["phase"] != "prematch":
            reason = "В снимке не подтверждён предматчевый статус. Доступен ИИ-разбор сохранённых рынков."
        elif canonical:
            if not quote or quote["odds"] != main_odds(document) or (quote["home"], quote["away"]) != (document["home"], document["away"]):
                reason = "Ожидаем доступный основной исход 1X2 и его сохранённый снимок."
            else:
                try:
                    model, _ = model_for(instant(quote["observed_at"]).date())
                    eligible = model.eligible(*canonical)
                    reason = "" if eligible else "Пока недостаточно истории этих команд в АПЛ."
                except ValueError as exc:
                    reason = str(exc)
        return {"prediction_available": eligible, "unavailable_reason": reason,
                "quote": {**quote, "model_teams": list(canonical)} if eligible else None}

    @app.get("/api/forecasts")
    def forecasts():
        return {"forecasts": repo.recent_forecasts()}

    @app.post("/api/bookmakers/events/{event_id}/explain")
    def explain_event(event_id: str, request: EventExplainRequest):
        return explain_event_impl(event_id,request,False)

    @app.post('/api/archive/events/{event_id}/explain')
    def explain_stream_event(event_id:str,request:EventExplainRequest):
        return explain_event_impl(event_id,request,True)

    def explain_event_impl(event_id,request,stream):
        from .event_analysis import analysis_context, request_key
        from .statistics import analysis_statistics
        if not settings.qwen_configured:
            raise HTTPException(503, "Сервис ИИ ещё не подключён.")
        if not event_id.isdigit() or len(event_id) > 20:
            raise HTTPException(422, "Некорректный ID события.")
        document = archive.document(repo,event_id) if stream else repo.event_markets(event_id)
        if not document:
            raise HTTPException(404, "Матч ещё не собран.")
        if ((stream and request.content_version and request.content_version!=document.get("content_version")) or
            (not stream and request.observed_at and request.observed_at != document["observed_at"])):
            raise HTTPException(409, "Снимок матча обновился. Обновите рынки и повторите запрос.")
        saved_statistics=stream_statistics(event_id)['statistics'] if stream else repo.statistics(event_id)
        if not stream and saved_statistics and saved_statistics['statistics_id']!=statistics_id_from_url(document.get('statistics_url') or ''):
            saved_statistics=None
        team_statistics=analysis_statistics(saved_statistics)
        archive_history=archive.history(repo,document)
        key = request_key(document, request.market_key, request.outcome_key, request.notes, team_statistics,archive_history)
        cached = repo.event_analysis(request_key=key)
        if cached and datetime.now(timezone.utc) - instant(cached["generated_at"]) < timedelta(minutes=10):
            return {**cached, "cached": True}
        if not analysis_lock.acquire(blocking=False):
            raise HTTPException(409, "ИИ уже готовит разбор. Дождитесь его завершения и повторите запрос.")
        try:
            statistics = None
            canonical = model_teams(document)
            if (not stream and canonical and document["phase"] == "prematch" and document["kickoff_at"]
                    and instant(document["kickoff_at"]) > datetime.now(timezone.utc)):
                try:
                    model, _ = model_for(min(instant(document["capture_started_at"]).date(), date.today()))
                    if model.eligible(*canonical):
                        statistics = model.predict(*canonical)
                except ValueError:
                    pass
            try:
                context = analysis_context(document, market_key=request.market_key, outcome_key=request.outcome_key,
                                           notes=request.notes, statistics=statistics,team_statistics=team_statistics,archive_history=archive_history)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            try:
                text = qwen.explain_event(settings, context)
            except ValueError as exc:
                raise HTTPException(502, public_ai_error(exc)) from exc
            selected = next((market for market in context["markets"] if market["key"] == request.market_key), None)
            payload = {"id": uuid4().hex, "generated_at": utc_now(), "event": context["event"],
                       "observed_at": document["observed_at"], "market_name": selected["name"] if selected else "Событие целиком",
                       "text": text, "context": context}
            repo.save_event_analysis(payload, key)
            return {**payload, "cached": False}
        finally:
            analysis_lock.release()

    @app.get("/api/analyses")
    def analyses():
        return {"analyses": repo.recent_event_analyses()}

    @app.get("/api/analyses/{analysis_id}")
    def analysis_detail(analysis_id: str):
        result = repo.event_analysis(analysis_id=analysis_id)
        if not result:
            raise HTTPException(404, "Разбор не найден.")
        return result

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
            raise HTTPException(503, "Сервис ИИ ещё не подключён.")
        saved = repo.forecast(request.forecast_id)
        if saved is None:
            raise HTTPException(404, "Прогноз не найден.")
        try:
            text = qwen.explain(settings, saved, request.notes)
        except ValueError as exc:
            raise HTTPException(502, public_ai_error(exc)) from exc
        return {"forecast_id": saved["id"], "text": text, "notes_verified": False}

    return app


app = create_app()
