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


PHOTO_STYLE = """Просьба о фото Миры, согласие на снимок или поправка к нему («так же, но…») → только
[[PHOTO]]{"scene":"…","caption":"…","prompt":"…","shot":"…","camera":"…","exclude":""}.
Обсуждение, цитата, отказ → беседа. scene: portrait — одета; boudoir или nude — по явной просьбе/согласию.
caption по-русски. prompt, shot, exclude на английском ASCII. prompt: сначала конкретные действие, поза,
одежда и место из просьбы. При поправке сохрани остальное из прошлого кадра; для nude убери одежду.
camera: front — селфи с руки; mirror — отражение с телефоном; timer — телефон на опоре для общего плана
или когда заняты руки; custom — заданная съёмка, например студийная. shot: крупность, угол телефона,
взгляд и источник света на месте. Для бытового кадра одна живая деталь: край плеча обрезан, блик или неровная
тень. Уважай заданный ракурс и свет. prompt + shot до 40 слов. exclude — явно исключённые предметы через
запятую. Береги личные данные; служебное описание не цитируй."""
# Put the essential camera geometry into the stored render prompt once, so a retry
# or a follow-up refers to the same composition. A custom shot is already specified.
CAMERAS = {
    "front": "handheld front-camera selfie, foreshortened forearm at frame edge",
    "mirror": "mirror selfie, phone visible in one hand in reflection",
    "timer": "phone propped up, self-timer snapshot",
    "custom": "",
}
DIRECT_REQUEST = re.compile(r"^(?:мира[,!]?[ ]+)?(?:пришли|скинь|вышли|отправь|покажи|сделай|можно)"
                            r"\s+(?:(?:мне|сво[еёю]|тво[еёю]|пожалуйста|полностью|голую|обнаж[её]нную|(?:интимн|откровенн|развратн)(?:ое|ую))\s+)*(?:фото|фотку|селфи|снимок)\b", re.I)


# A generic adult or nude photo request keeps its existing meaning.
EXPLICIT_SELFIE = re.compile(
    r"^(?:мира[,!]?\s+)?(?:(?:пришли|скинь|хочу|сделай|покажи|нужно|давай|можно)\s+)?"
    r"(?:(?:мне|прям|прямо|сво[её])\s+)*"
    r"(?:порно(?:[- ]|,?\s+но\s+)?селфи|селфи\s+(?:в стиле|в формате)\s+порно)"
    r"(?=$|[\s.,!)—-])", re.I)
EXPLICIT_PLAN = ("Для этой прямой просьбы верни [[PHOTO]] и JSON с scene=explicit. Это селфи одной "
                 "совершеннолетней Миры: сохрани в prompt запрошенный взрослый жанр и действие, "
                 "выбери camera=front или mirror по смыслу. Остальные поля по правилам фото.")


DECLINE = re.compile(r"^\s*(?:мира[, ]+)?(?:(?:фото|фотку|снимок|селфи)\s+не\s+(?:надо|хочу|присылай)|"
                     r"не\s+(?:присылай|показывай|хочу|надо)\s+(?:мне\s+)?(?:фото|фотку|снимок|селфи))\b", re.I)
INTIMATE = re.compile(r"\b(?:18\+|ню\b|обнаж\w*|гол(?:ая|ую|ой|еньк\w*)|без (?:одежды|белья)|"
                      r"в (?:бель[её]|трус\w*|лифчик\w*)|раздет\w*|разден\w*|интим\w*|эроти\w*|нюд\w*|"
                      r"топлес|nudes?\b|naked|lingerie|underwear|раздень\w*)", re.I)
ADULT_PHOTO = re.compile(r"\b(?:откровенн|развратн)(?:ое|ую)\s+(?:фото|фотку|селфи)\b", re.I)
AGREES = re.compile(r"^\s*(?:да\b|давай\b|хочу\b|пришли\b|скинь\b|покажи\b|конечно\b|ага\b|ещ[её] одну\b)", re.I)


# Only the user's explicit request or a short correction to a delivered photo
# requires this scene. Discussion about nudity is still an ordinary chat turn.
NUDITY = r"(?:голую|обнаж[её]нную|без одежды|ню)"
NUDE_EDIT = re.compile(rf"(?:(?:хочу|давай|только|полностью|совсем|тебя)\s+)*{NUDITY}"
                       rf"(?:[\s,–—-]+(?:полностью\s+)?{NUDITY})?[.! )]*", re.I)
NUDE_WORD = re.compile(rf"\b{NUDITY}\b", re.I)
NEGATED_NUDITY = re.compile(rf"\b(?:не|кроме|никак\w*)\s+(?:(?:хочу|надо|нужно|присылай|полностью)\s+)*{NUDITY}\b", re.I)
PRIVATE_PHOTO = re.compile(r"(?:\[?Доставлено фото Миры\s*[,：:]|Справка для следующего кадра\s*\(|"
                           r'["«]подпись["»]\s*:\s*"[^"\n]*"\s*,\s*"описание"\s*:|'
                           r'"prompt"\s*:\s*"[^"\n]*"\s*,\s*"shot"\s*:)', re.I)


def asks_explicit_selfie(message):
    match = EXPLICIT_SELFIE.match(message)
    if not match:
        return False
    rest = message[match.end():].strip(" ,.!)—-").lower()
    if re.search(r"\b(?:не|нет|отмена|передумал\w*|отмен\w*|без порно)\b", rest):
        return False
    return not rest or rest == "пожалуйста" or rest.startswith(
        ("в ", "на ", "у ", "из ", "с ", "при ", "лежа ", "лёжа ", "сидя ", "стоя "))


def requires_nude_scene(message, after_photo):
    if NEGATED_NUDITY.search(message):
        return False
    return bool((DIRECT_REQUEST.match(message) and NUDE_WORD.search(message))
                or (after_photo and NUDE_EDIT.fullmatch(message)))


def clean_old_reply(text):
    marker = PRIVATE_PHOTO.search(text)
    if not marker:
        return text
    return text[:marker.start()].rstrip(" \n[{") or "Мне не удалось подготовить снимок."


REPEAT_PHOTO = re.compile(r"(?:давай\s+)?(?:ещ[её] (?:одну|одно|такую же)|тот же кадр|так(?:ую)? же)"
                          r"(?:[, ]+(?:но |только )?(?:в зеркале|сбоку|сверху|снизу|поближе|издалека))?[.! )]*", re.I)
DECLINE_ADULT = re.compile(r"\b(?:без (?:интима|эротики|ню)|(?:только|лучше) в одежде|"
                          r"не (?:интимн|откровенн|развратн)\w*)\b", re.I)


def wants_intimate_photo(message, last_answer, *, previous_scene=None, requested_scene=None):
    if NEGATED_NUDITY.search(message) or DECLINE_ADULT.search(message):
        return False
    if requested_scene in ADULT and previous_scene == requested_scene and REPEAT_PHOTO.fullmatch(message):
        return True
    direct = DIRECT_REQUEST.match(message)
    requested_style = asks_explicit_selfie(message) or (direct and ADULT_PHOTO.search(direct.group()))
    return bool(requested_style or INTIMATE.search(message) or
                (AGREES.match(message) and INTIMATE.search(last_answer)
                 and re.search(r"фото|снимок|селфи|показать|прислать", last_answer, re.I)))


class PhotoPlanError(ValueError):
    """Validation reasons contain only fixed labels, safe to use in service logs."""


PHOTO_PUNCTUATION = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "‑": "-"})


def photo_text(value):
    if isinstance(value, str):
        return " ".join(value.translate(PHOTO_PUNCTUATION).split())
    return value


def read_plan(answer, *, allow_json=False, require_nude=False, explicit_selfie=False):
    raw = answer.strip()
    marked = "[[PHOTO]]" in raw
    if marked:
        raw = raw.split("[[PHOTO]]", 1)[1].strip()
    elif not raw.startswith(("{", "```")):
        return None
    if raw.startswith("```") and raw.endswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)[:-3].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        if marked or allow_json:
            raise PhotoPlanError("Invalid photo JSON") from exc
        return None
    if not (marked or allow_json) and not (isinstance(data, dict) and {"scene", "caption", "prompt"} <= data.keys()):
        return None
    scenes = ("explicit",) if explicit_selfie else ("portrait", "boudoir", "nude")
    if not isinstance(data, dict) or data.get("scene") not in scenes:
        raise PhotoPlanError("Invalid photo scene")
    prompt, caption = photo_text(data.get("prompt")), data.get("caption")
    if not isinstance(prompt, str) or not re.fullmatch(r"[ -~]{3,400}", prompt) or not re.search("[a-zA-Z]", prompt):
        raise PhotoPlanError("Invalid photo description")
    if not isinstance(caption, str) or not caption.strip() or len(caption) > 120 or not caption.isprintable():
        raise PhotoPlanError("Invalid photo caption")
    shot = photo_text(data.get("shot"))
    if not isinstance(shot, str) or not re.fullmatch(r"[ -~]{3,160}", shot) or not re.search("[a-zA-Z]", shot):
        raise PhotoPlanError("Invalid camera description")
    prompt = prompt.strip() + ", " + shot.strip()
    exclude = photo_text(data.get("exclude", ""))
    if not isinstance(exclude, str) or not re.fullmatch(r"[ -~]{0,120}", exclude):
        raise PhotoPlanError("Invalid photo exclusions")
    camera = data.get("camera", "custom")
    if isinstance(camera, str):
        camera = camera.strip().lower().replace("_", " ").replace("-", " ")
        camera = {"front camera": "front", "selfie": "front", "mirror selfie": "mirror",
                  "self timer": "timer", "tripod": "timer"}.get(camera, camera)
    if not isinstance(camera, str) or camera not in CAMERAS:
        raise PhotoPlanError("Invalid camera mode")
    if explicit_selfie and camera not in ("front", "mirror"):
        raise PhotoPlanError("Expected a selfie camera")
    if CAMERAS[camera]:
        prompt = CAMERAS[camera] + ", " + prompt
    if camera == "front":
        exclude = ", ".join(filter(None, (exclude.strip(), "mirror selfie, phone blocking face")))
    scene = data["scene"]
    if scene == "portrait":
        if re.search(r"\b(nude|naked|topless|bare breasts)\b", prompt, re.I):
            scene = "nude"
        elif re.search(r"\b(lingerie|underwear)\b", prompt, re.I):
            scene = "boudoir"
    if PRIVATE_PHOTO.search(caption):
        raise PhotoPlanError("Internal photo caption")
    if explicit_selfie:
        # Older clients use "nude" to conceal the image until opened. The
        # renderer receives the requested genre and full composition in text.
        return "nude", "Sexually explicit adult selfie, " + prompt, caption.strip(), exclude.strip()
    if require_nude:
        outfit = re.search(r"\b(?:clothed|dressed in|knit(?:ted)? top|lingerie|bra|sweater|t-shirt)\b", prompt, re.I)
        if scene != "nude" or outfit or re.search(r"\b(?:nudity|nude|naked)\b", exclude, re.I):
            raise PhotoPlanError("Photo scene contradicts request")
        prompt = "Unclothed adult woman, " + prompt
        exclude = ", ".join(filter(None, ("clothing, underwear", exclude.strip())))
    return scene, prompt, caption.strip(), exclude.strip()


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

    def enqueue(db, user_id, key, scene, photo_prompt=None, caption=None, negative_prompt=None):
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
        db.execute("INSERT INTO companion_photos(id,user_id,scene,created_at,state,deadline,photo_prompt,caption,negative_prompt) "
                   "VALUES (?,?,?,?,'queued',?,?,?,?)", (key, user_id, scene, created, now + 600, photo_prompt, caption, negative_prompt))
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
                old = db.execute(f"SELECT {METADATA},photo_prompt,negative_prompt FROM companion_photos WHERE id=? AND user_id=?",
                                 (data["retry_of"], user["id"])).fetchone()
                if not old or public(old, time.time())["state"] != "failed":
                    raise HTTPException(409, "Снимок уже обработан.")
                return enqueue(db, user["id"], key, old["scene"], old["photo_prompt"], old["caption"], old["negative_prompt"])
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
            row = db.execute("SELECT id,scene,photo_prompt,negative_prompt FROM companion_photos WHERE state='queued' AND deadline>? "
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
