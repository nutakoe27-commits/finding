"""Private photo messages. The GPU worker claims jobs over outgoing HTTPS."""
import hmac
import json
import re
import time
from uuid import UUID, uuid4

from fastapi import HTTPException, Request, Response

from . import accounts, companion_admin

SCENES = {"selfie": "селфи у окна", "cafe": "портрет в кафе",
          "boudoir": "портрет в белье", "nude": "портрет ню"}
ADULT = {"boudoir", "nude"}
MAX_JPEG = 800_000
METADATA = "id,scene,created_at,state,deadline,caption"


PHOTO_STYLE = """Фото Миры: если человек просит снимок сейчас (также «покажись там», «ещё одну» или согласие на предложение фото),
верни только [[PHOTO]] и JSON {"scene":"portrait","caption":"У моря","prompt":"red dress, evening seaside"}.
Подпись по-русски; prompt — до 40 английских слов: пожелания человека и детали сцены из разговора,
без личных данных. Одна взрослая Мира. portrait — одета; boudoir или nude только по явной интимной просьбе
или согласию на конкретный интимный снимок. Обсуждение, цитата и отказ от фото — обычная беседа."""
DIRECT_REQUEST = re.compile(r"^(?:мира[,!]?[ ]+)?(?:пришли|скинь|вышли|отправь|покажи|сделай|можно)"
                            r"\s+(?:(?:мне|сво[еёю]|тво[еёю]|пожалуйста)\s+)*(?:фото|фотку|селфи|снимок)\b", re.I)


DECLINE = re.compile(r"^\s*(?:мира[, ]+)?(?:(?:фото|фотку|снимок|селфи)\s+не\s+(?:надо|хочу|присылай)|"
                     r"не\s+(?:присылай|показывай|хочу|надо)\s+(?:мне\s+)?(?:фото|фотку|снимок|селфи))\b", re.I)
INTIMATE = re.compile(r"\b(?:18\+|ню\b|обнаж\w*|гол(?:ая|ую|ой|еньк\w*)|без (?:одежды|белья)|"
                      r"в (?:бель[её]|трус\w*|лифчик\w*)|раздет\w*|разден\w*|интим\w*|эроти\w*|нюд\w*|"
                      r"топлес|nudes?\b|naked|lingerie|underwear|раздень\w*)", re.I)
AGREES = re.compile(r"^\s*(?:да\b|давай\b|хочу\b|пришли\b|скинь\b|покажи\b|конечно\b|ага\b|ещ[её] одну\b)", re.I)


def wants_intimate_photo(message, last_answer):
    return bool(INTIMATE.search(message) or
                (AGREES.match(message) and INTIMATE.search(last_answer)
                 and re.search(r"фото|снимок|селфи|показать|прислать", last_answer, re.I)))


def read_plan(answer, *, allow_json=False):
    raw = answer.strip()
    if "[[PHOTO]]" in raw:
        raw = raw.split("[[PHOTO]]", 1)[1].strip()
    elif not allow_json or not raw.startswith(("{", "```")):
        return None
    if raw.startswith("```") and raw.endswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)[:-3].strip()
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("scene") not in ("portrait", "boudoir", "nude"):
        raise ValueError("Invalid photo scene")
    prompt, caption = data.get("prompt"), data.get("caption")
    if not isinstance(prompt, str) or not re.fullmatch(r"[ -~]{3,400}", prompt) or not re.search("[a-zA-Z]", prompt):
        raise ValueError("Invalid photo description")
    if not isinstance(caption, str) or not caption.strip() or len(caption) > 120 or not caption.isprintable():
        raise ValueError("Invalid photo caption")
    scene = data["scene"]
    if scene == "portrait" and re.search(r"\b(nude|naked|topless|lingerie|underwear|bare breasts)\b", prompt, re.I):
        scene = "nude"
    return scene, prompt.strip(), caption.strip()


def public(row, now):
    state = row["state"]
    if state in ("queued", "running") and row["deadline"] <= now:
        state = "failed"
    return {**{k: row[k] for k in ("id", "scene", "created_at", "caption")}, "state": state}


def install(app, repo, settings):
    keyfile = settings.data_dir / "photo-worker.key"

    heartbeat = settings.data_dir / "photo-worker.alive"

    def last_poll(path):
        try:
            return path.stat().st_mtime
        except FileNotFoundError:
            return 0

    def worker(request):
        try:
            key = keyfile.read_text().strip()
        except FileNotFoundError:
            key = ""
        supplied = request.headers.get("authorization", "").encode()
        if len(key) < 32 or not hmac.compare_digest(supplied, ("Bearer " + key).encode()):
            raise HTTPException(403)

    context_heartbeat = settings.data_dir / "photo-worker-context.alive"

    def enqueue(db, user_id, key, scene, photo_prompt=None, caption=None):
        now = int(time.time())
        row = db.execute(f"SELECT {METADATA},user_id FROM companion_photos WHERE id=?", (key,)).fetchone()
        if row:
            if row["user_id"] != user_id or row["scene"] != scene:
                raise HTTPException(409, "Повторите запрос.")
            return public(row, now)
        if now - last_poll(context_heartbeat if photo_prompt else heartbeat) > 300:
            raise HTTPException(503, "Мира пока не может прислать фото. Попробуйте позже.")
        active = db.execute("SELECT user_id FROM companion_photos WHERE "
                            "state IN ('queued','running') AND deadline>?", (now,)).fetchall()
        if any(r["user_id"] == user_id for r in active):
            raise HTTPException(409, "Ваш снимок уже готовится.")
        if len(active) >= 8:
            raise HTTPException(429, "Очередь заполнена. Попробуйте чуть позже.")
        created = accounts.now()
        db.execute("INSERT INTO companion_photos(id,user_id,scene,created_at,state,deadline,photo_prompt,caption) "
                   "VALUES (?,?,?,?,'queued',?,?,?)", (key, user_id, scene, created, now + 600, photo_prompt, caption))
        for old in db.execute("SELECT id FROM companion_photos WHERE user_id=? "
                              "ORDER BY created_at DESC,id DESC", (user_id,)).fetchall()[40:]:
            db.execute("DELETE FROM companion_photos WHERE id=?", (old["id"],))
        companion_admin.record(db, user_id, "photo_requested")
        return {"id": key, "scene": scene, "caption": caption, "created_at": created, "state": "queued"}

    @app.get("/api/chat/photos")
    def photos(request: Request):
        user = accounts.current_user(request)
        now = int(time.time())
        with repo.connection() as db:
            rows = db.execute(f"SELECT {METADATA} FROM companion_photos WHERE user_id=? "
                              "ORDER BY created_at,id", (user["id"],)).fetchall()
        return {"enabled": keyfile.is_file(), "photos": [public(row, now) for row in rows]}

    @app.post("/api/chat/photos", status_code=202)
    async def create(request: Request):
        user = accounts.current_user(request)
        accounts.mutation(request)
        if not accounts.agreements_current(repo, user["id"]):
            raise HTTPException(403, "Подтвердите условия общения.")
        if not keyfile.is_file():
            raise HTTPException(503, "Фотографии пока недоступны.")
        data = await accounts.body(request)
        try:
            key = str(UUID(str(data.get("id"))))
        except ValueError:
            raise HTTPException(422, "Некорректный номер запроса.")
        if "retry_of" in data:
            if not isinstance(data["retry_of"], str):
                raise HTTPException(422)
            with repo.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                old = db.execute(f"SELECT {METADATA},photo_prompt FROM companion_photos WHERE id=? AND user_id=?",
                                 (data["retry_of"], user["id"])).fetchone()
                if not old or public(old, time.time())["state"] != "failed":
                    raise HTTPException(409, "Снимок уже обработан.")
                return enqueue(db, user["id"], key, old["scene"], old["photo_prompt"], old["caption"])
        scene = data.get("scene")
        if not isinstance(scene, str) or scene not in SCENES:
            raise HTTPException(422, "Выберите сюжет.")
        if scene in ADULT and data.get("adult") is not True:
            raise HTTPException(422, "Подтвердите выбор фото 18+.")
        with repo.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            return enqueue(db, user["id"], key, scene)

    @app.get("/api/chat/photos/{key}/image")
    def image(key: str, request: Request):
        user = accounts.current_user(request)
        with repo.connection() as db:
            row = db.execute("SELECT jpeg FROM companion_photos WHERE id=? AND user_id=? AND state='ready'",
                             (key, user["id"])).fetchone()
        if not row:
            raise HTTPException(404)
        return Response(row["jpeg"], media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})

    @app.post("/api/photo-worker/claim")
    def claim(request: Request):
        worker(request)
        now = int(time.time())
        contextual = request.headers.get("X-Mira-Photo-Context") == "1"
        for path in ((heartbeat, context_heartbeat) if contextual else (heartbeat,)):
            if now - last_poll(path) > 25:
                path.touch()
        compatible = "" if contextual else "AND photo_prompt IS NULL "
        # Idle polls need no write lock. Recheck eligibility in the conditional update.
        with repo.connection() as db:
            row = db.execute("SELECT id,scene,photo_prompt FROM companion_photos WHERE state='queued' AND deadline>? "
                             f"{compatible}ORDER BY created_at,id LIMIT 1", (now,)).fetchone()
        if not row:
            return Response(status_code=204)
        claim_id = str(uuid4())
        with repo.connection() as db:
            updated = db.execute("UPDATE companion_photos SET state='running',claim_id=?,deadline=? "
                                 "WHERE id=? AND state='queued' AND deadline>?",
                                 (claim_id, now + 300, row["id"], now))
        if not updated.rowcount:
            return Response(status_code=204)
        return {**dict(row), "claim": claim_id}

    @app.post("/api/photo-worker/{key}/{claim_id}")
    async def complete(key: str, claim_id: str, request: Request):
        worker(request)
        jpeg = bytearray()
        async for chunk in request.stream():
            if len(jpeg) + len(chunk) > MAX_JPEG:
                raise HTTPException(413)
            jpeg.extend(chunk)
        if jpeg and (request.headers.get("content-type") != "image/jpeg" or
                     not jpeg.startswith(b"\xff\xd8\xff") or not jpeg.endswith(b"\xff\xd9")):
            raise HTTPException(422)
        with repo.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,deadline FROM companion_photos WHERE id=? AND claim_id=?",
                             (key, claim_id)).fetchone()
            if not row or (row["state"] == "running" and row["deadline"] <= time.time()):
                raise HTTPException(410)
            if row["state"] == "running":
                db.execute("UPDATE companion_photos SET state=?,jpeg=? WHERE id=?",
                           ("ready" if jpeg else "failed", bytes(jpeg) if jpeg else None, key))
        return Response(status_code=204)

    return enqueue
