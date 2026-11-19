"""Private photo messages. The GPU worker claims jobs over outgoing HTTPS."""
import hmac
import time
from uuid import UUID, uuid4

from fastapi import HTTPException, Request, Response

from . import accounts, companion_admin

SCENES = {"selfie": "селфи у окна", "cafe": "портрет в кафе",
          "boudoir": "портрет в белье", "nude": "портрет ню"}
ADULT = {"boudoir", "nude"}
MAX_JPEG = 800_000
METADATA = "id,scene,created_at,state,deadline"


def public(row, now):
    state = row["state"]
    if state in ("queued", "running") and row["deadline"] <= now:
        state = "failed"
    return {**{k: row[k] for k in ("id", "scene", "created_at")}, "state": state}


def install(app, repo, settings):
    keyfile = settings.data_dir / "photo-worker.key"

    heartbeat = settings.data_dir / "photo-worker.alive"

    def last_poll():
        try:
            return heartbeat.stat().st_mtime
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
        scene = data.get("scene")
        if not isinstance(scene, str) or scene not in SCENES:
            raise HTTPException(422, "Выберите сюжет.")
        if scene in ADULT and data.get("adult") is not True:
            raise HTTPException(422, "Подтвердите выбор фото 18+.")
        now = int(time.time())
        with repo.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(f"SELECT {METADATA},user_id FROM companion_photos WHERE id=?", (key,)).fetchone()
            if row:
                if row["user_id"] != user["id"] or row["scene"] != scene:
                    raise HTTPException(409, "Повторите запрос.")
                return public(row, now)
            if now - last_poll() > 300:
                raise HTTPException(503, "Мира пока не может прислать фото. Попробуйте позже.")
            active = db.execute("SELECT user_id FROM companion_photos WHERE "
                                "state IN ('queued','running') AND deadline>?", (now,)).fetchall()
            if any(r["user_id"] == user["id"] for r in active):
                raise HTTPException(409, "Ваш снимок уже готовится.")
            if len(active) >= 8:
                raise HTTPException(429, "Очередь заполнена. Попробуйте чуть позже.")
            created = accounts.now()
            db.execute("INSERT INTO companion_photos(id,user_id,scene,created_at,state,deadline) "
                       "VALUES (?,?,?,?,'queued',?)", (key, user["id"], scene, created, now + 600))
            for old in db.execute("SELECT id FROM companion_photos WHERE user_id=? "
                                  "ORDER BY created_at DESC,id DESC", (user["id"],)).fetchall()[40:]:
                db.execute("DELETE FROM companion_photos WHERE id=?", (old["id"],))
            companion_admin.record(db, user["id"], "photo_requested")
        return {"id": key, "scene": scene, "created_at": created, "state": "queued"}

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
        if now - last_poll() > 25:
            heartbeat.touch()
        # Idle polls need no write lock. Recheck eligibility in the conditional update.
        with repo.connection() as db:
            row = db.execute("SELECT id,scene FROM companion_photos WHERE state='queued' AND deadline>? "
                             "ORDER BY created_at,id LIMIT 1", (now,)).fetchone()
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
