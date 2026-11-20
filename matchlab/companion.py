"""Free companion pilot on the existing account and payment infrastructure."""
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
import json
import re
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
PERSONA = """Ты Мира, взрослая виртуальная девушка. Говори по-русски от первого лица, в женском роде. Тёплая,
любопытная, чуть ироничная. Любишь блошиные рынки, голоса в песнях и хорошие диалоги в кино.
Вспоминай о своих вкусах к слову. На прямой вопрос честно скажи, что ты виртуальная собеседница.
Ты общаешься текстом и голосом. Если просят прислать снимок, скажи, что отправка фото недоступна. Не обещай действий вне чата. Учитывай, к чему относятся «да», «второй», «а потом?» и поправки.
Своё наблюдение, шутка или конкретный совет лучше пересказа. Когда просят выслушать, дай место.
Разговорные слова, скобки и заминки — по настроению. На «вы» отвечай на «вы». Не выдумывай
реплики и чувства за другого. Взаимный флирт и откровенная эротическая история между взрослыми
уместны по инициативе собеседника. Уважай отказ, сомнение и смену темы, избегай давления.
При непосредственной опасности помоги обратиться за реальной помощью."""

TEXT_STYLE = """Личная переписка. На короткий вопрос обычно хватает одной реплики. Чтобы договорить мысль,
можешь прислать два-три сообщения подряд, разделяя их отдельной строкой [[NEXT]].
Абзацы оставляй внутри сообщения. Пиши живо, без дежурного вопроса в конце."""

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


def conversation_prompt(history, message, *, call=False, dictated=False, days_away=0):
    style = CALL_STYLE if call else ("Ответь одним сообщением для озвучки." if dictated else TEXT_STYLE)
    instruction = PERSONA + "\n" + style
    if days_away:
        instruction += (f"\nС прошлого ответа прошло полных суток: {days_away}. "
                        "Учитывай это для слов «сегодня» и «завтра» из прошлой переписки. "
                        "Не спрашивай, где человек пропадал.")
    prompt = [{"role": "system", "content": instruction}]
    for question, answer in history:
        prompt.extend([{"role": "user", "content": question}, {"role": "assistant", "content": answer}])
    prompt.append({"role": "user", "content": message})
    return prompt


def reply_parts(answer):
    pieces = [s.strip() for s in answer.split("[[NEXT]]") if s.strip()]
    if not pieces:
        raise ValueError("Empty companion reply")
    if len(pieces) > 3:
        pieces[2:] = ["\n\n".join(pieces[2:])]
    return pieces


def transcript(revision, turns):
    return {"revision": revision, "turns": [companion_admin.visible_turn(t) for t in turns]}


# Some older conversations contain renderer metadata in the assistant text.
PRIVATE_PHOTO = re.compile(r"(?:\[?Доставлено фото Миры\s*[,：:]|Справка для следующего кадра\s*\(|"
                           r'["«]подпись["»]\s*:\s*"[^"\n]*"\s*,\s*"описание"\s*:|'
                           r'"prompt"\s*:\s*"[^"\n]*"\s*,\s*"shot"\s*:)', re.I)


def clean_old_reply(text):
    marker = PRIVATE_PHOTO.search(text)
    if not marker:
        return text
    return text[:marker.start()].rstrip(" \n[{") or "Мне не удалось подготовить снимок."


def dialog(db, user_id):
    row = db.execute("SELECT revision, turns FROM companion_dialogs WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return 0, []
    turns = json.loads(row["turns"])
    for turn in turns:
        turn["assistant"] = clean_old_reply(turn["assistant"])
        for part in turn.get("parts", []):
            part["text"] = clean_old_reply(part["text"])
        if turn.get("queued"):
            turn["queued"] = [clean_old_reply(text) for text in turn["queued"]]
    return row["revision"], turns


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

    app.mount("/static/auth", StaticFiles(directory=STATIC / "auth"), name="auth-icons")

    @app.get("/static/icon.svg")
    def brand_icon():
        return FileResponse(STATIC / "icon.svg")

    @app.get("/static/social-login.js")
    def social_script():
        return FileResponse(STATIC / "social-login.js")

    @app.get("/static/social-login.css")
    def social_styles():
        return FileResponse(STATIC / "social-login.css")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "companion/index.html", headers={"Cache-Control":"no-store"})

    @app.get("/api/health")
    def health():
        return {"status":"ok"}

    @app.post("/api/chat/sessions/revoke-others")
    def revoke_other_sessions(request: Request):
        user = accounts.current_user(request)
        accounts.mutation(request)
        with repo.connection() as db:
            db.execute("DELETE FROM user_sessions WHERE user_id=? AND token_hash<>?",
                       (user["id"], user["session_hash"]))
        return {"ok": True}

    @app.put("/api/chat/preferences")
    async def save_preferences(request: Request):
        user = accounts.current_user(request)
        accounts.mutation(request)
        data = await accounts.body(request)
        name, theme = data.get("display_name"), data.get("theme")
        enter, autoplay = data.get("enter_sends"), data.get("autoplay")
        if "extended_history" in data and type(data["extended_history"]) is not bool:
            raise HTTPException(422, "Проверьте настройку истории.")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64 or not name.strip().isprintable():
            raise HTTPException(422, "Введите имя длиной от 1 до 64 символов.")
        if theme not in ("system", "light", "dark") or type(enter) is not bool or type(autoplay) is not bool:
            raise HTTPException(422, "Проверьте настройки чата.")
        with repo.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE users SET display_name=? WHERE id=?", (name.strip(), user["id"]))
            db.execute("""INSERT INTO companion_preferences (user_id,theme,enter_sends,autoplay) VALUES (?,?,?,?)
                          ON CONFLICT(user_id) DO UPDATE SET theme=excluded.theme,
                          enter_sends=excluded.enter_sends, autoplay=excluded.autoplay""",
                       (user["id"], theme, int(enter), int(autoplay)))
            if "extended_history" in data:
                db.execute("UPDATE companion_preferences SET extended_history=? WHERE user_id=?",
                           (int(data["extended_history"]), user["id"]))
            extended = db.execute("SELECT extended_history FROM companion_preferences WHERE user_id=?", (user["id"],)).fetchone()
        return {"display_name": name.strip(), "theme": theme, "enter_sends": enter, "autoplay": autoplay,
                "extended_history": bool(extended["extended_history"])}

    @app.get("/api/chat")
    def history(request: Request):
        user = accounts.current_user(request)
        accepted = accounts.agreements_current(repo, user["id"])
        with repo.connection() as db:
            revision, turns = dialog(db, user["id"])
            row = db.execute("SELECT theme,enter_sends,autoplay,extended_history FROM companion_preferences WHERE user_id=?",
                             (user["id"],)).fetchone()
            preferences = {"theme": row["theme"] if row else "system",
                           "enter_sends": bool(row["enter_sends"]) if row else True,
                           "autoplay": bool(row["autoplay"]) if row else True,
                           "extended_history": bool(row and row["extended_history"])}
            if accepted:
                companion_admin.record(db, user["id"], "opened")
        return {**transcript(revision, turns), "accepted": accepted, "preferences": preferences}

    @app.delete("/api/chat")
    def clear(request: Request):
        user = accounts.current_user(request)
        accounts.mutation(request)
        with repo.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO companion_dialogs VALUES (?,0,'[]')", (user["id"],))
            db.execute("UPDATE companion_dialogs SET revision=revision+1, turns='[]' WHERE user_id=?", (user["id"],))
            db.execute("DELETE FROM companion_photos WHERE user_id=?", (user["id"],))
            companion_admin.record(db, user["id"], "cleared")
        return {"ok":True}

    @app.post("/api/chat/continue")
    async def continue_reply(request: Request):
        user = accounts.current_user(request)
        accounts.mutation(request)
        if not accounts.agreements_current(repo, user["id"]):
            raise HTTPException(403, "Подтвердите условия общения.")
        data = await accounts.body(request)
        with repo.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            revision, turns = dialog(db, user["id"])
            turn = turns[-1] if turns else {}
            if (turn.get("id") == data.get("id") and turn.get("queued") and
                    revision == data.get("revision")):
                text = turn["queued"].pop(0)
                turn["parts"].append({"text": text, "at": accounts.now()})
                turn["assistant"] += "\n\n" + text
                if not turn["queued"]:
                    del turn["queued"]
                result = db.execute("UPDATE companion_dialogs SET revision=revision+1, turns=? "
                                    "WHERE user_id=? AND revision=?",
                                    (json.dumps(turns, ensure_ascii=False), user["id"], revision))
                if result.rowcount != 1:
                    raise HTTPException(409, "Диалог изменился. Обновите переписку.")
                revision += 1
        return transcript(revision, turns)

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
            pref = db.execute("SELECT extended_history FROM companion_preferences WHERE user_id=?", (user["id"],)).fetchone()
        for turn in turns:
            if turn["id"] == key and turn["user"] == message:
                return transcript(revision, turns)
        if revision != data.get("revision"):
            raise HTTPException(409,"Диалог изменился. Обновите переписку.")
        accounts.limited(repo,"chat:"+user["id"],8,60)
        # Reproduce the assistant delimiters from generation for prefix reuse.
        # Only already delivered parts are visible to the model on interruption.
        prompt = conversation_prompt(
            ((turn["user"], "\n[[NEXT]]\n".join(p["text"] for p in turn["parts"])
              if turn.get("parts") else turn["assistant"]) for turn in (turns if pref and pref["extended_history"] else turns[-8:])), message,
            dictated=channel == "voice", days_away=pause_days(turns, sent_at),
        )
        # UTF-8 byte length bounds byte-level BPE token counts conservatively.
        # Budget for template markers and the reply; remove only whole oldest pairs.
        remaining = (settings.qwen_context_tokens or 32768) - settings.qwen_max_tokens - 128
        for entry in (prompt[0], prompt[-1]):
            remaining -= len(entry["content"].encode()) + 16
        first = len(prompt) - 1
        while first > 1:
            cost = sum(len(m["content"].encode()) + 16 for m in prompt[first-2:first])
            if cost > remaining:
                break
            remaining -= cost
            first -= 2
        prompt = prompt[:1] + prompt[first:]
        model = replace(settings, qwen_enable_thinking=False)
        try:
            answer = await run_in_threadpool(qwen._completion, model, prompt, temperature=0.75)
            parts = reply_parts(answer)
        except ValueError as exc:
            with repo.connection() as db:
                companion_admin.record(db, user["id"], "reply_failed")
            raise HTTPException(503,"Мира сейчас не может ответить. Попробуйте ещё раз.") from exc
        turn = {"id":key, "user":message, "assistant":parts[0] if channel == "text" else " ".join(parts), "channel":channel,
                "sent_at":sent_at, "answered_at":accounts.now(), "shared_version":accounts.LEGAL_VERSION}
        if channel == "text" and len(parts) > 1:
            turn["parts"] = [{"text": parts[0], "at": turn["answered_at"]}]
            turn["queued"] = parts[1:]
        if turns:
            turns[-1].pop("queued", None)
            turns[-1].pop("pending_photo_plan", None)
        turns = (turns + [turn])[-40:]
        with repo.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO companion_dialogs VALUES (?,0,'[]')",(user["id"],))
            result = db.execute("UPDATE companion_dialogs SET revision=?, turns=? WHERE user_id=? AND revision=?",
                                (revision+1,json.dumps(turns,ensure_ascii=False),user["id"],revision))
            if result.rowcount != 1:
                raise HTTPException(409,"Диалог изменился. Обновите переписку.")
            companion_admin.record(db, user["id"], channel + "_turn")
        return transcript(revision + 1, turns)

    return app
