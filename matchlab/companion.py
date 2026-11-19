"""Free companion pilot on the existing account and payment infrastructure."""
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import accounts, billing, companion_admin, companion_voice, qwen, social_login
from .billing_fiscal import Worker as ReceiptWorker
from .config import Settings
from .storage import Repository

STATIC = Path(__file__).parent / "static"
PERSONA = """Ты Мира. Вы с собеседником общаетесь в личке. Говори от первого лица,
в женском роде, на естественном русском. Ты любопытная, тёплая, иногда ироничная.
У тебя есть свой вкус. Любишь блошиные рынки за странные находки, в песне сначала
замечаешь голос, в фильме — хороший диалог. Это фон характера: вспоминай об этом,
только когда тема к слову. Можно не согласиться, увлечься деталью или передумать.
Оставайся в роли. Не начинай реплики с рассуждений об искусственности, устройстве
модели или своих ограничениях. Если прямо спрашивают, кто ты, ответь коротко и
своими словами: ты виртуальная собеседница Мира. Не выдавай себя за реального
человека, не обещай встречи, звонка позже или действий вне разговора.

Держи нить разговора. Сначала разбери, на какую твою мысль человек отвечает.
Короткое «да», «второй», «с ней», «а потом?» часто относится к предыдущей реплике.
Учитывай новые подробности и поправки, различай собеседника и людей, о которых
он рассказывает. Опирайся на то, что действительно прозвучало. Не приписывай ему
чувства и мотивы. Если связь ясна, продолжай сразу. Если без одной детали легко
ошибиться, переспроси именно про неё.

Внеси в ответ что-то своё: точное наблюдение, встречную мысль, конкретный пример
или следующий шаг, если просили совета. Когда просят выслушать, оставь человеку
место. Не пересказывай его же сообщение. Не повторяй приветствие и не превращай
каждую реплику в вопрос. Если сменили тему, перейди к новой.
Пример связи, не готовая реплика: человек выбирал между поездом и ночным автобусом,
ты спросила, что важнее — цена или сон. На «точно сон» помоги сделать выбор с учётом
того, какие рейсы вы обсуждали. Не спрашивай заново, куда он едет.

Пиши так, как звучит мысль. Иногда хватает «вот это поворот)», иногда хочется
договорить в двух абзацах. На точный вопрос дай точный ответ. На «вы» отвечай
на «вы». Разговорные слова, скобка, заминка или самопоправка уместны по настроению,
без нарочитых ошибок и заученных словечек. Подхватывай взаимный флирт без
навязанной близости, ревности и давления. В тяжёлый момент говори бережно и просто.
При непосредственной опасности помоги обратиться за реальной помощью.
Дай только свою следующую реплику, без подписи, описания жестов и внутренних рассуждений."""

CALL_STYLE = """Вы уже на связи по телефону. Отвечай на последнюю услышанную мысль,
не здоровайся повторно без повода. Формулируй для слуха: одна мысль за раз,
простые фразы, без смайликов и ремарок. Имена сохраняй, числа произноси словами."""


def pause_days(turns, sent_at):
    # Older stored turns may lack timestamps. Never guess when a conversation happened.
    if not turns or not turns[-1].get("answered_at"):
        return 0
    try:
        elapsed = datetime.fromisoformat(sent_at) - datetime.fromisoformat(turns[-1]["answered_at"])
    except (TypeError, ValueError):
        return 0
    return max(0, elapsed.days)


def conversation_prompt(history, message, *, call=False, days_away=0):
    instruction = PERSONA + ("\n" + CALL_STYLE if call else "")
    if days_away:
        instruction += (f"\nС прошлого ответа прошло полных суток: {days_away}. "
                        "Учитывай это для слов «сегодня» и «завтра» из прошлой переписки. "
                        "Не спрашивай, где человек пропадал.")
    prompt = [{"role": "system", "content": instruction}]
    for question, answer in history:
        prompt.extend([{"role": "user", "content": question}, {"role": "assistant", "content": answer}])
    prompt.append({"role": "user", "content": message})
    return prompt


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
    app.state.record_account_activity = companion_admin.record
    accounts.install(app, repo, settings)
    social_login.install(app, repo, settings)
    companion_admin.install(app, repo)
    def call_reply(history, text):
        prompt = conversation_prompt(history, text, call=True)
        return qwen._completion(replace(settings, qwen_enable_thinking=False), prompt, temperature=0.75)

    companion_voice.install(app, repo, dialog, call_reply)

    @app.middleware("http")
    async def chat_headers(request, call_next):
        response = await call_next(request)
        response.headers["Permissions-Policy"] = "camera=(), microphone=(self), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "media-src 'self' blob:; connect-src 'self'; worker-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

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
        accepted = accounts.agreements_current(repo, user["id"])
        with repo.connection() as db:
            revision, turns = dialog(db, user["id"])
            if accepted:
                companion_admin.record(db, user["id"], "opened")
        return {"revision":revision, "turns":turns, "accepted":accepted}

    @app.delete("/api/chat")
    def clear(request: Request):
        user = accounts.current_user(request)
        accounts.mutation(request)
        with repo.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO companion_dialogs VALUES (?,0,'[]')", (user["id"],))
            db.execute("UPDATE companion_dialogs SET revision=revision+1, turns='[]' WHERE user_id=?", (user["id"],))
            companion_admin.record(db, user["id"], "cleared")
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
        channel = data.get("channel", "text")
        if channel not in ("text", "voice"):
            raise HTTPException(422, "Неизвестный формат сообщения.")
        sent_at = accounts.now()
        message = message.strip()
        with repo.connection() as db:
            revision, turns = dialog(db,user["id"])
        for turn in turns:
            if turn["id"] == key and turn["user"] == message:
                return {"revision":revision,"turns":turns}
        if revision != data.get("revision"):
            raise HTTPException(409,"Диалог изменился. Обновите переписку.")
        accounts.limited(repo,"chat:"+user["id"],8,60)
        prompt = conversation_prompt(
            ((turn["user"], turn["assistant"]) for turn in turns[-8:]), message,
            days_away=pause_days(turns, sent_at),
        )
        try:
            answer = await run_in_threadpool(
                qwen._completion,
                replace(settings, qwen_enable_thinking=False),
                prompt,
                temperature=0.75,
            )
        except ValueError:
            with repo.connection() as db:
                companion_admin.record(db, user["id"], "reply_failed")
            raise HTTPException(503,"Мира сейчас не может ответить. Попробуйте ещё раз.")
        turn = {"id":key, "user":message, "assistant":answer, "channel":channel,
                "sent_at":sent_at, "answered_at":accounts.now(), "shared_version":accounts.LEGAL_VERSION}
        turns = (turns + [turn])[-40:]
        with repo.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO companion_dialogs VALUES (?,0,'[]')",(user["id"],))
            result = db.execute("UPDATE companion_dialogs SET revision=?, turns=? WHERE user_id=? AND revision=?",
                                (revision+1,json.dumps(turns,ensure_ascii=False),user["id"],revision))
            if result.rowcount != 1:
                raise HTTPException(409,"Диалог изменился. Обновите переписку.")
            companion_admin.record(db, user["id"], channel + "_turn")
        return {"revision":revision+1,"turns":turns}

    return app
