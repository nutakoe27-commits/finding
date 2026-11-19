"""Free companion pilot on the existing account and payment infrastructure."""
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import accounts, billing, qwen, social_login
from .billing_fiscal import Worker as ReceiptWorker
from .config import Settings
from .storage import Repository

STATIC = Path(__file__).parent / "static"
PERSONA = """Ты Мира, взрослая виртуальная собеседница. Пользователь знает, что ты ИИ.
Отвечай по-русски, учитывай тон и контекст разговора. Будь тёплой, живой и любопытной,
уважай границы собеседника. Не выдавай себя за человека, не выдумывай совместных
воспоминаний и не дави на пользователя, чтобы он продолжал общение."""


def dialog(db, user_id):
    row = db.execute("SELECT revision, turns FROM companion_dialogs WHERE user_id=?", (user_id,)).fetchone()
    return (row["revision"], json.loads(row["turns"])) if row else (0, [])


def create_chat_app(settings=None):
    settings = settings or Settings.from_env()
    repo = Repository(settings.database)
    receipts = ReceiptWorker(repo, settings)

    @asynccontextmanager
    async def lifespan(app):
        repo.initialize()
        billing.initialize(repo, settings)
        receipts.start()
        try:
            yield
        finally:
            receipts.stop()

    app = FastAPI(title="Мира", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    accounts.install(app, repo, settings)
    social_login.install(app, repo, settings)
    # Reuse settlement, receipt and cancellation handlers for existing orders.
    finance = APIRouter()
    billing.install(finance, repo, settings)
    allowed = {"/api/billing/wallet", "/api/billing/orders/{identifier}",
               "/api/billing/yookassa/webhook", "/api/billing/subscriptions/{identifier}/cancel"}
    app.router.routes.extend(r for r in finance.routes if r.path in allowed)
    app.mount("/chat-assets", StaticFiles(directory=STATIC / "companion"), name="chat-assets")
    app.mount("/static/legal", StaticFiles(directory=STATIC / "legal"), name="legal")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "companion/index.html", headers={"Cache-Control":"no-store"})

    @app.get("/api/health")
    def health():
        return {"status":"ok"}

    @app.get("/api/chat")
    def history(request: Request):
        user = accounts.current_user(request)
        with repo.connection() as db:
            revision, turns = dialog(db, user["id"])
        return {"revision":revision, "turns":turns, "accepted":accounts.agreements_current(repo,user["id"])}

    @app.delete("/api/chat")
    def clear(request: Request):
        user = accounts.current_user(request)
        accounts.mutation(request)
        with repo.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO companion_dialogs VALUES (?,0,'[]')", (user["id"],))
            db.execute("UPDATE companion_dialogs SET revision=revision+1, turns='[]' WHERE user_id=?", (user["id"],))
        return {"ok":True}

    @app.post("/api/chat")
    async def send(request: Request):
        user = accounts.current_user(request)
        accounts.mutation(request)
        if not accounts.agreements_current(repo,user["id"]):
            raise HTTPException(403,"Подтвердите условия общения.")
        data = await accounts.body(request, 18000)
        message = data.get("message")
        if not isinstance(message,str) or not 1 <= len(message.strip()) <= 2000:
            raise HTTPException(422,"Введите от 1 до 2000 символов.")
        try:
            key = str(UUID(str(data.get("id"))))
        except ValueError:
            raise HTTPException(422,"Обновите страницу.")
        message = message.strip()
        with repo.connection() as db:
            revision, turns = dialog(db,user["id"])
        for turn in turns:
            if turn["id"] == key and turn["user"] == message:
                return {"revision":revision,"turns":turns}
        if revision != data.get("revision"):
            raise HTTPException(409,"Диалог изменился. Обновите переписку.")
        accounts.limited(repo,"chat:"+user["id"],8,60)
        prompt = [{"role":"system","content":PERSONA}]
        for turn in turns[-8:]:
            prompt.extend([{"role":"user","content":turn["user"]},{"role":"assistant","content":turn["assistant"]}])
        prompt.append({"role":"user","content":message})
        try:
            answer = await run_in_threadpool(qwen._completion, replace(settings,qwen_enable_thinking=False,
                qwen_max_tokens=min(650,settings.qwen_max_tokens)),prompt)
        except ValueError:
            raise HTTPException(503,"Мира сейчас не может ответить. Попробуйте ещё раз.")
        turns = (turns + [{"id":key,"user":message,"assistant":answer}])[-40:]
        with repo.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO companion_dialogs VALUES (?,0,'[]')",(user["id"],))
            result = db.execute("UPDATE companion_dialogs SET revision=?, turns=? WHERE user_id=? AND revision=?",
                                (revision+1,json.dumps(turns,ensure_ascii=False),user["id"],revision))
            if result.rowcount != 1:
                raise HTTPException(409,"Диалог изменился. Обновите переписку.")
        return {"revision":revision+1,"turns":turns}

    return app
