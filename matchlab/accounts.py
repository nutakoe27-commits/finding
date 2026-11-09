"""Local accounts, opaque revocable sessions, request guards and AI usage."""

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from threading import BoundedSemaphore
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY,username TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,
 role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user','admin')),
 enabled INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,last_login_at TEXT,
 bonus_analyses INTEGER NOT NULL DEFAULT 0 CHECK(bonus_analyses>=0));
CREATE TABLE IF NOT EXISTS user_identities (
 provider TEXT NOT NULL,subject TEXT NOT NULL,user_id TEXT NOT NULL,
 PRIMARY KEY(provider,subject));
CREATE TABLE IF NOT EXISTS user_sessions (
 token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,created_at TEXT NOT NULL,
 expires_at TEXT NOT NULL,elevated_at TEXT);
CREATE INDEX IF NOT EXISTS sessions_user ON user_sessions(user_id);
CREATE TABLE IF NOT EXISTS request_limits (
 bucket TEXT PRIMARY KEY,start INTEGER NOT NULL,hits INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ai_usage (
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL,at TEXT NOT NULL,kind TEXT NOT NULL,
 target TEXT NOT NULL,status TEXT NOT NULL,latency_ms INTEGER,used_bonus INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS usage_user_date ON ai_usage(user_id,at);
CREATE TABLE IF NOT EXISTS admin_audit (
 id INTEGER PRIMARY KEY,at TEXT NOT NULL,actor_id TEXT,action TEXT NOT NULL,
 target TEXT,details TEXT NOT NULL);
"""
HASH_SLOTS = BoundedSemaphore(3)
AI_ROUTE = re.compile(
    r"^/api/(?:explain|(?:bookmakers|archive)/events/[^/]+/explain)/?$"
)


def now():
    return datetime.now(UTC).isoformat(timespec="microseconds")


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    with HASH_SLOTS:
        result = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt),
            n=32768,
            r=8,
            p=1,
            maxmem=64 * 1024 * 1024,
            dklen=32,
        )
    return f"scrypt$32768$8$1${salt}${result.hex()}"


def password_matches(password, stored):
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if (scheme, n, r, p) != ("scrypt", "32768", "8", "1"):
            return False
        return hmac.compare_digest(
            password_hash(password, salt).rsplit("$", 1)[1], digest
        )
    except (ValueError, TypeError):
        return False


def validate_credentials(username, password):
    if not isinstance(username, str) or not re.fullmatch(
        r"[a-zA-Z0-9_]{3,32}", username
    ):
        raise HTTPException(
            422, "Логин: 3–32 латинские буквы, цифры или подчёркивание."
        )
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise HTTPException(422, "Пароль должен содержать от 12 до 128 символов.")
    return username.casefold()


def audit(db, actor, action, target=None, details=None):
    db.execute(
        "INSERT INTO admin_audit(at,actor_id,action,target,details) VALUES (?,?,?,?,?)",
        (now(), actor, action, target, json.dumps(details or {}, ensure_ascii=False)),
    )


def limited(repo, bucket, limit, seconds):
    stamp = int(time.time())
    start = stamp // seconds * seconds
    with repo.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO request_limits VALUES (?,?,1) ON CONFLICT(bucket) DO UPDATE SET hits=CASE WHEN start=excluded.start THEN hits+1 ELSE 1 END,start=excluded.start",
            (token_hash(bucket), start),
        )
        count = db.execute(
            "SELECT hits FROM request_limits WHERE bucket=?", (token_hash(bucket),)
        ).fetchone()[0]
        db.execute("DELETE FROM request_limits WHERE start<?", (stamp - 86400,))
    if count > limit:
        raise HTTPException(
            429,
            "Слишком много попыток. Попробуйте позже.",
            headers={"Retry-After": str(seconds - stamp % seconds)},
        )


def peer(request):
    host = request.client.host if request.client else "unknown"
    if host in ("127.0.0.1", "::1"):
        # Nginx overwrites this header; the app must listen on loopback only.
        host = request.headers.get("x-real-ip", host)[:100]
    return host


def origin(request):
    return request.app.state.settings.public_origin or str(request.base_url).rstrip("/")


def cookie_name(request):
    return (
        "__Host-prognozich"
        if origin(request).startswith("https://")
        else "prognozich_session"
    )


def csrf(token):
    return hmac.new(token.encode(), b"prognozich:csrf:v1", hashlib.sha256).hexdigest()


def mutation(request, authenticated=True):
    if (
        request.headers.get("content-type", "").split(";")[0].lower()
        != "application/json"
    ):
        raise HTTPException(415, "Нужен JSON-запрос.")
    supplied = request.headers.get("origin")
    if supplied and supplied != origin(request):
        raise HTTPException(403, "Запрос должен быть отправлен со страницы сервиса.")
    if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
        raise HTTPException(403, "Запрос должен быть отправлен со страницы сервиса.")
    if authenticated:
        token = request.cookies.get(cookie_name(request), "")
        if not token or not hmac.compare_digest(
            request.headers.get("x-csrf-token", ""), csrf(token)
        ):
            raise HTTPException(403, "Обновите страницу и повторите действие.")


def current_user(request, required=True):
    token = request.cookies.get(cookie_name(request), "")
    with request.app.state.repository.connection() as db:
        row = (
            db.execute(
                "SELECT u.*,s.token_hash AS session_hash,s.created_at AS session_created_at,s.elevated_at FROM user_sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.enabled=1",
                (token_hash(token), now()),
            ).fetchone()
            if token
            else None
        )
    if not row and required:
        raise HTTPException(
            401, "Войдите или зарегистрируйтесь, чтобы получить разбор ИИ."
        )
    return dict(row) if row else None


def admin_user(request, elevated=False):
    user = current_user(request)
    if user["role"] != "admin":
        raise HTTPException(403, "Этот раздел доступен администратору.")
    if elevated and (
        not user["elevated_at"]
        or datetime.fromisoformat(user["elevated_at"])
        < datetime.now(UTC) - timedelta(minutes=10)
    ):
        raise HTTPException(
            403,
            "Подтвердите пароль администратора перед этим действием.",
            headers={"X-Reauth-Required": "1"},
        )
    return user


def make_session(repo, user_id):
    token = secrets.token_urlsafe(32)
    with repo.connection() as db:
        db.execute("DELETE FROM user_sessions WHERE expires_at<=?", (now(),))
        db.execute(
            "INSERT INTO user_sessions VALUES (?,?,?,?,NULL)",
            (
                token_hash(token),
                user_id,
                now(),
                (datetime.now(UTC) + timedelta(days=7)).isoformat(
                    timespec="microseconds"
                ),
            ),
        )
        db.execute("UPDATE users SET last_login_at=? WHERE id=?", (now(), user_id))
        # A finite number of sessions also limits stale devices after password changes.
        db.execute(
            "DELETE FROM user_sessions WHERE user_id=? AND token_hash NOT IN (SELECT token_hash FROM user_sessions WHERE user_id=? ORDER BY created_at DESC LIMIT 10)",
            (user_id, user_id),
        )
    return token


def public_user(user, settings):
    return {
        k: user[k] for k in ("id", "username", "role", "created_at", "bonus_analyses")
    } | {"daily_limit": settings.ai_daily_limit}


def authenticated_response(request, user_id, status=200, old_token=None):
    repo = request.app.state.repository
    if old_token:
        with repo.connection() as db:
            db.execute(
                "DELETE FROM user_sessions WHERE token_hash=?", (token_hash(old_token),)
            )
    token = make_session(repo, user_id)
    with repo.connection() as db:
        user = dict(db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())
    response = JSONResponse(
        {"user": public_user(user, request.app.state.settings), "csrf": csrf(token)},
        status_code=status,
    )
    response.set_cookie(
        cookie_name(request),
        token,
        max_age=7 * 86400,
        httponly=True,
        secure=origin(request).startswith("https://"),
        samesite="lax",
        path="/",
    )
    return response


async def body(request, limit=16384):
    chunks = []
    size = 0
    async for part in request.stream():
        size += len(part)
        if size > limit:
            raise HTTPException(413, "Запрос слишком большой.")
        chunks.append(part)
    try:
        value = json.loads(b"".join(chunks))
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(400, "Нужен JSON-объект.") from exc
    if not isinstance(value, dict):
        raise HTTPException(422, "Нужен JSON-объект.")
    return value


def create_user(repo, username, password, role="user"):
    name = validate_credentials(username, password)
    user_id = uuid4().hex
    encoded = password_hash(password)
    try:
        with repo.connection() as db:
            db.execute(
                "INSERT INTO users(id,username,password_hash,role,created_at) VALUES (?,?,?,?,?)",
                (user_id, name, encoded, role, now()),
            )
            db.execute(
                "INSERT INTO user_identities VALUES ('local',?,?)", (name, user_id)
            )
            if role == "admin":
                audit(db, user_id, "admin.bootstrap", user_id)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "Этот логин уже занят.") from exc
    return user_id


def start_usage(repo, user, kind, target, settings):
    usage_id = uuid4().hex
    bonus = 0
    with repo.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        expired = (datetime.now(UTC) - timedelta(minutes=10)).isoformat(
            timespec="microseconds"
        )
        abandoned = db.execute(
            "SELECT id,user_id,used_bonus FROM ai_usage WHERE user_id=? AND status='pending' AND at<?",
            (user["id"], expired),
        ).fetchall()
        for old in abandoned:
            db.execute("UPDATE ai_usage SET status='failed' WHERE id=?", (old["id"],))
            if old["used_bonus"]:
                db.execute(
                    "UPDATE users SET bonus_analyses=bonus_analyses+1 WHERE id=?",
                    (old["user_id"],),
                )
        if settings.ai_daily_limit:
            count = db.execute(
                "SELECT count(*) FROM ai_usage WHERE user_id=? AND at>=? AND (status='success' OR (status='pending' AND at>?)) AND used_bonus=0",
                (
                    user["id"],
                    datetime.now(UTC).date().isoformat(),
                    (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
                ),
            ).fetchone()[0]
            if count >= settings.ai_daily_limit:
                result = db.execute(
                    "UPDATE users SET bonus_analyses=bonus_analyses-1 WHERE id=? AND bonus_analyses>0",
                    (user["id"],),
                )
                if not result.rowcount:
                    raise HTTPException(
                        429,
                        "Дневной лимит разборов исчерпан. Следующие доступны завтра.",
                    )
                bonus = 1
        db.execute(
            "INSERT INTO ai_usage(id,user_id,at,kind,target,status,used_bonus) VALUES (?,?,?,?,?,'pending',?)",
            (usage_id, user["id"], now(), kind, target[:100], bonus),
        )
    return usage_id


def finish_usage(repo, usage_id, status, latency_ms):
    with repo.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM ai_usage WHERE id=?", (usage_id,)).fetchone()
        if not row or row["status"] != "pending":
            return
        db.execute(
            "UPDATE ai_usage SET status=?,latency_ms=? WHERE id=?",
            (status, latency_ms, usage_id),
        )
        if status != "success" and row["used_bonus"]:
            db.execute(
                "UPDATE users SET bonus_analyses=bonus_analyses+1 WHERE id=?",
                (row["user_id"],),
            )


def install(app, repo, settings):
    app.state.repository = repo

    @app.middleware("http")
    async def guard(request, call_next):
        usage = None
        started = time.monotonic()
        try:
            path = request.url.path
            if AI_ROUTE.fullmatch(path) and request.method == "POST":
                user = current_user(request)
                mutation(request)
                limited(repo, "ai:" + user["id"], 6, 60)
                request.state.user = user
                usage = start_usage(
                    repo,
                    user,
                    "event" if "/events/" in path else "forecast",
                    path,
                    settings,
                )
            elif path == "/api/analyses" or path.startswith("/api/analyses/"):
                request.state.user = current_user(request)
            response = await call_next(request)
        except HTTPException as exc:
            response = JSONResponse(
                {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
            )
        except Exception:
            if usage:
                finish_usage(
                    repo, usage, "failed", int((time.monotonic() - started) * 1000)
                )
            raise
        if usage:
            finish_usage(
                repo,
                usage,
                "success" if response.status_code < 400 else "failed",
                int((time.monotonic() - started) * 1000),
            )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; worker-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/auth/me")
    def me(request: Request):
        user = current_user(request, False)
        return {
            "user": public_user(user, settings) if user else None,
            "csrf": csrf(request.cookies[cookie_name(request)]) if user else None,
            "providers": ["local"],
        }

    @app.post("/api/auth/register")
    async def register(request: Request):
        mutation(request, False)
        limited(repo, "register:" + peer(request), 5, 3600)
        data = await body(request)
        user_id = await run_in_threadpool(
            create_user, repo, data.get("username"), data.get("password")
        )
        return authenticated_response(
            request, user_id, 201, request.cookies.get(cookie_name(request))
        )

    @app.post("/api/auth/login")
    async def login(request: Request):
        mutation(request, False)
        limited(repo, "login-ip:" + peer(request), 20, 60)
        data = await body(request)
        name = validate_credentials(data.get("username"), data.get("password"))
        limited(repo, "login-user:" + name, 10, 900)
        with repo.connection() as db:
            row = db.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
        # Unknown accounts perform the same password derivation; the digest is not a credential.
        stored = (
            row["password_hash"]
            if row
            else "scrypt$32768$8$1$" + "0" * 32 + "$" + "0" * 64
        )
        valid = await run_in_threadpool(password_matches, data["password"], stored)
        if not valid or not row or not row["enabled"]:
            raise HTTPException(401, "Неверный логин или пароль.")
        return authenticated_response(
            request, row["id"], old_token=request.cookies.get(cookie_name(request))
        )

    @app.post("/api/auth/logout")
    def logout(request: Request):
        current_user(request)
        mutation(request)
        with repo.connection() as db:
            db.execute(
                "DELETE FROM user_sessions WHERE token_hash=?",
                (token_hash(request.cookies[cookie_name(request)]),),
            )
        response = JSONResponse({"ok": True})
        response.delete_cookie(
            cookie_name(request),
            path="/",
            secure=origin(request).startswith("https://"),
            httponly=True,
            samesite="lax",
        )
        return response

    @app.post("/api/auth/password")
    async def change_password(request: Request):
        user = current_user(request)
        mutation(request)
        limited(repo, "password:" + user["id"], 5, 900)
        data = await body(request)
        validate_credentials(user["username"], data.get("new_password"))
        if not isinstance(data.get("password"), str) or not await run_in_threadpool(
            password_matches, data["password"], user["password_hash"]
        ):
            raise HTTPException(401, "Неверный текущий пароль.")
        encoded = await run_in_threadpool(password_hash, data["new_password"])
        with repo.connection() as db:
            db.execute(
                "UPDATE users SET password_hash=? WHERE id=?", (encoded, user["id"])
            )
            db.execute("DELETE FROM user_sessions WHERE user_id=?", (user["id"],))
            audit(db, user["id"], "account.password_changed", user["id"])
        return authenticated_response(request, user["id"])
