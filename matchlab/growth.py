"""First-party analytics, referral links, promotional credits and admin operations."""

import re
import json
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlsplit
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from . import accounts, archive

SCHEMA = """
CREATE TABLE IF NOT EXISTS product_events (
 id INTEGER PRIMARY KEY,at TEXT NOT NULL,user_id TEXT,visitor TEXT NOT NULL,
 name TEXT NOT NULL,entity TEXT);
CREATE INDEX IF NOT EXISTS product_events_date ON product_events(at,name);
CREATE TABLE IF NOT EXISTS analytics_consents (
 token_hash TEXT PRIMARY KEY,user_id TEXT,version TEXT NOT NULL,accepted_at TEXT NOT NULL,revoked_at TEXT,
 webvisor INTEGER NOT NULL DEFAULT 0 CHECK(webvisor IN (0,1)));
CREATE TABLE IF NOT EXISTS promo_codes (
 id TEXT PRIMARY KEY,code TEXT NOT NULL UNIQUE,title TEXT NOT NULL,
 bonus INTEGER NOT NULL CHECK(bonus>0),max_uses INTEGER NOT NULL CHECK(max_uses>0),
 starts_at TEXT NOT NULL,ends_at TEXT NOT NULL,status TEXT NOT NULL,
 created_at TEXT NOT NULL,created_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS promo_redemptions (
 promo_id TEXT NOT NULL,user_id TEXT NOT NULL,at TEXT NOT NULL,bonus INTEGER NOT NULL,
 PRIMARY KEY(promo_id,user_id));
CREATE TABLE IF NOT EXISTS site_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS push_subscriptions (
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL,endpoint TEXT NOT NULL,keys_json TEXT NOT NULL,
 created_at TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,last_status TEXT);
CREATE INDEX IF NOT EXISTS push_users ON push_subscriptions(user_id,enabled);
CREATE TABLE IF NOT EXISTS push_ad_consents (
 subscription_id TEXT NOT NULL,user_id TEXT NOT NULL,version TEXT NOT NULL,accepted_at TEXT NOT NULL,revoked_at TEXT,
 PRIMARY KEY(subscription_id,user_id));
CREATE TABLE IF NOT EXISTS push_campaigns (
 id TEXT PRIMARY KEY,title TEXT NOT NULL,body TEXT NOT NULL,url TEXT NOT NULL,
 segment TEXT NOT NULL,status TEXT NOT NULL,scheduled_at TEXT NOT NULL,
 created_at TEXT NOT NULL,created_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS push_outbox (
 campaign_id TEXT NOT NULL,subscription_id TEXT NOT NULL,status TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0,next_at TEXT NOT NULL,lease_until TEXT,
 lease_id TEXT,last_status TEXT,sent_at TEXT,PRIMARY KEY(campaign_id,subscription_id));
CREATE INDEX IF NOT EXISTS push_due ON push_outbox(status,next_at);
CREATE TABLE IF NOT EXISTS push_worker_status (id INTEGER PRIMARY KEY CHECK(id=1),at TEXT NOT NULL,state TEXT NOT NULL,error_type TEXT);
"""
EVENTS = {"page_view", "match_view", "ai_intent", "push_click", "outbound_click"}
ANALYTICS_VERSION = '2026-11-09.metrika1'


def setting(repo, key, default=""):
    with repo.connection() as db:
        row = db.execute(
            "SELECT value FROM site_settings WHERE key=?", (key,)
        ).fetchone()
    return row[0] if row else default


def visitor(request):
    cookie = request.cookies.get("prognozich_visitor", "")
    return cookie if re.fullmatch(r"[a-f0-9]{32}", cookie) else uuid4().hex


def analytics_preferences(repo, request):
    token=request.cookies.get('prognozich_analytics','')
    if token=='no': return {'analytics':False,'webvisor':False}
    if not re.fullmatch(r'v1\.[A-Za-z0-9_-]{32}',token): return {'analytics':None,'webvisor':False}
    with repo.connection() as db:
        cutoff=(datetime.now(UTC)-timedelta(days=365)).isoformat(timespec='microseconds')
        row=db.execute('SELECT webvisor FROM analytics_consents WHERE token_hash=? AND version=? AND revoked_at IS NULL AND accepted_at>?',
                       (accounts.token_hash(token),ANALYTICS_VERSION,cutoff)).fetchone()
    return {'analytics':True,'webvisor':bool(row['webvisor'])} if row else {'analytics':None,'webvisor':False}


def analytics_allowed(repo, request):
    return analytics_preferences(repo,request)['analytics'] is True


def record(repo, name, entity=None, user_id=None, visitor_id="server"):
    with repo.connection() as db:
        db.execute(
            "INSERT INTO product_events(at,user_id,visitor,name,entity) VALUES (?,?,?,?,?)",
            (accounts.now(), user_id, accounts.token_hash(visitor_id), name, entity),
        )


def text(value, size=200, empty=False):
    if (
        not isinstance(value, str)
        or len(value) > size
        or (not empty and not value.strip())
    ):
        raise HTTPException(422, "Проверьте заполнение полей.")
    return value.strip()


def integer(value, low=1, high=100000):
    if type(value) is not int or not low <= value <= high:
        raise HTTPException(422, "Число вне допустимого диапазона.")
    return value


def date_time(value):
    try:
        at = datetime.fromisoformat(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(422, "Укажите дату и время с часовым поясом.") from exc
    if at.tzinfo is None:
        raise HTTPException(422, "Укажите часовой пояс.")
    return at.astimezone(UTC).isoformat(timespec="microseconds")


def outbound(repo, event_id, source="stream"):
    if not re.fullmatch(r"[0-9]{1,20}", event_id):
        raise HTTPException(404, "Событие не найдено.")
    doc = (
        archive.document(repo, event_id)
        if source == "stream"
        else repo.event_markets(event_id)
    )
    if not doc:
        raise HTTPException(404, "Событие не найдено.")
    fallback = f"https://winline.ru/stavki/event/{event_id}"
    url = doc.get("url") or fallback
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ("winline.ru", "www.winline.ru")
        or parsed.username
        or not parsed.path.startswith("/stavki/")
    ):
        url = fallback
    template = setting(repo, "winline_referral")
    return (
        template.replace("{url}", quote(url, safe="")).replace("{event_id}", event_id)
        if template
        else url
    )


def referral_template(value):
    value = text(value, 2000, True)
    if not value:
        return ""
    unknown = re.findall(r"\{([^}]+)\}", value)
    if any(k not in ("url", "event_id") for k in unknown) or "{" in re.sub(
        r"\{(?:url|event_id)\}", "", value
    ):
        raise HTTPException(422, "Доступны подстановки {url} и {event_id}.")
    parsed = urlsplit(
        value.replace("{url}", "https%3A%2F%2Fwinline.ru").replace("{event_id}", "123")
    )
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or any(c in value for c in ("\r", "\n", "\\"))
        or "." not in parsed.hostname
    ):
        raise HTTPException(422, "Нужна HTTPS-ссылка партнёрской программы.")
    return value


def usage_details(db,row):
    """Prefer per-request diagnostics; recover old rows only from one matching saved analysis."""
    if row['details']:
        details=json.loads(row['details'])
        if details.get('reason')=='context_limit' and details.get('analysis_id'):
            saved=db.execute('SELECT payload FROM event_analyses WHERE id=? AND user_id=?',
                             (details['analysis_id'],row['user_id'])).fetchone()
            if saved:
                from .qwen import generation_reason
                gen=json.loads(saved['payload']).get('context',{}).get('ai_generation') or {}
                reason=generation_reason(gen)
                if reason=='quotation_mismatch':details.update(reason=reason,corrected_from_saved=True)
        return details
    match=re.fullmatch(r'/api/(?:bookmakers|archive)/events/(\d+)/explain',row['target'])
    if not match or row['status']!='limited':return None
    candidates=db.execute('''SELECT payload FROM event_analyses WHERE user_id=?
        AND json_extract(payload,'$.event.source_event_id')=?
        AND julianday(generated_at)>=julianday(?)
        AND (julianday(generated_at)-julianday(?))*86400<=?''',
        (row['user_id'],match[1],row['at'],row['at'],(row['latency_ms'] or 0)/1000+2)).fetchall()
    if len(candidates)!=1:return None
    saved=json.loads(candidates[0]['payload']);gen=saved.get('context',{}).get('ai_generation') or {}
    if not gen.get('state'):return None
    from .qwen import generation_reason
    return {key:gen.get(key) for key in ('state','reason','model_calls','parts_total','parts_completed','markets_total','markets_completed')} | {'reason':generation_reason(gen),'analysis_id':saved['id'],'derived_from_saved':True}


def overview(repo, days):
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(
        timespec="microseconds"
    )
    with repo.connection() as db:
        scalar = lambda sql, args=(): db.execute(sql, args).fetchone()[0]
        users = scalar("SELECT count(*) FROM users")
        usage = dict(
            db.execute(
                """SELECT count(*) total,coalesce(sum(status='success'),0) success,
                   coalesce(sum(status='failed' OR json_extract(details,'$.state') IN ('unavailable','partial')),0) failed,
                   coalesce(sum(status='limited'),0) limited,
                   coalesce(sum(json_extract(details,'$.model_calls')),0) model_calls,
                   coalesce(sum(json_extract(details,'$.cache_hit')=1),0) cached,
                   coalesce(round(avg(CASE WHEN status='success' AND coalesce(json_extract(details,'$.cache_hit'),0)=0 THEN latency_ms END)),0) avg_ms
                   FROM ai_usage WHERE at>=?""",
                (cutoff,),
            ).fetchone()
        )
        counters = {
            r["name"]: r["total"]
            for r in db.execute(
                "SELECT name,count(*) total FROM product_events WHERE at>=? GROUP BY name",
                (cutoff,),
            )
        }
        daily = [
            dict(r)
            for r in db.execute(
                "SELECT substr(at,1,10) day,count(*) views,count(DISTINCT visitor) visitors FROM product_events WHERE at>=? AND name='page_view' GROUP BY day ORDER BY day",
                (cutoff,),
            )
        ]
        result = {
            "days": days,
            "users": users,
            "new_users": scalar(
                "SELECT count(*) FROM users WHERE created_at>=?", (cutoff,)
            ),
            "active_users": scalar(
                "SELECT count(*) FROM users WHERE last_login_at>=?", (cutoff,)
            ),
            "visitors": scalar(
                "SELECT count(DISTINCT visitor) FROM product_events WHERE at>=? AND name='page_view'",
                (cutoff,),
            ),
            "events": counters,
            "ai": usage,
            "daily": daily,
            "push_subscribers": scalar(
                "SELECT count(DISTINCT s.user_id) FROM push_subscriptions s JOIN users u ON u.id=s.user_id JOIN push_ad_consents a ON a.subscription_id=s.id AND a.user_id=s.user_id AND a.revoked_at IS NULL WHERE s.enabled=1 AND u.enabled=1"
            ),
            "push_sent": scalar(
                "SELECT count(*) FROM push_outbox WHERE status='sent' AND sent_at>=?",
                (cutoff,),
            ),
            "promo_activations": scalar(
                "SELECT count(*) FROM promo_redemptions WHERE at>=?", (cutoff,)
            ),
            "promo_credits": scalar(
                "SELECT coalesce(sum(bonus),0) FROM promo_redemptions WHERE at>=?",
                (cutoff,),
            ),
        }
        result['recent_ai']=[]
        for row in db.execute('SELECT a.*,u.username FROM ai_usage a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.at DESC LIMIT 12').fetchall():
            result['recent_ai'].append({key:row[key] for key in ('at','username','kind','status','latency_ms')} | {'generation':usage_details(db,row)})
    result["archive"] = archive.overview(repo)
    return result


def install(app, repo, settings):
    @app.get('/api/privacy/preferences')
    def privacy_preferences(request: Request):
        return analytics_preferences(repo,request)

    @app.post('/api/privacy/preferences')
    async def update_privacy_preferences(request: Request):
        accounts.mutation(request, False)
        accounts.limited(repo,'privacy:'+accounts.peer(request),60,60)
        data=await accounts.body(request, 1024)
        enabled=data.get('analytics')
        webvisor=data.get('webvisor',False)
        if type(enabled) is not bool or type(webvisor) is not bool or (webvisor and not enabled):
            raise HTTPException(422,'Выберите настройку аналитики.')
        from fastapi.responses import JSONResponse
        response=JSONResponse({'analytics':enabled,'webvisor':webvisor})
        old=request.cookies.get('prognozich_analytics','')
        user=accounts.current_user(request,False) if enabled else None
        with repo.connection() as db:
            if re.fullmatch(r'v1\.[A-Za-z0-9_-]{32}',old):
                db.execute('UPDATE analytics_consents SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL',(accounts.now(),accounts.token_hash(old)))
            token='v1.'+secrets.token_urlsafe(24) if enabled else 'no'
            if enabled:
                db.execute('INSERT INTO analytics_consents(token_hash,user_id,version,accepted_at,revoked_at,webvisor) VALUES (?,?,?,?,NULL,?)',
                           (accounts.token_hash(token),user['id'] if user else None,ANALYTICS_VERSION,accounts.now(),int(webvisor)))
        response.set_cookie('prognozich_analytics',token,max_age=365*86400,
                            httponly=True,samesite='lax',secure=accounts.origin(request).startswith('https://'),path='/')
        if not enabled:
            response.delete_cookie('prognozich_visitor',path='/')
        return response

    @app.post("/api/telemetry")
    async def telemetry(request: Request):
        accounts.mutation(request, False)
        accounts.limited(repo, "telemetry:" + accounts.peer(request), 600, 60)
        data = await accounts.body(request, 4096)
        if data.get("name") not in EVENTS - {"outbound_click"}:
            raise HTTPException(422, "Неизвестное событие.")
        entity = text(data.get("entity", ""), 100, True)
        # Only route identifiers are collected; notes, searches, URLs and passwords are never telemetry.
        if entity and not re.fullmatch(r"[#a-zA-Z0-9_:/-]{1,100}", entity):
            raise HTTPException(422, "Некорректный идентификатор.")
        if not analytics_allowed(repo,request):
            return {'ok':True}
        user = accounts.current_user(request, False)
        cookie = visitor(request)
        record(repo, data["name"], entity, user["id"] if user else None, cookie)
        from fastapi.responses import JSONResponse

        response = JSONResponse({"ok": True})
        if request.cookies.get("prognozich_visitor") != cookie:
            response.set_cookie(
                "prognozich_visitor",
                cookie,
                max_age=30 * 86400,
                httponly=True,
                samesite="lax",
                secure=accounts.origin(request).startswith("https://"),
                path="/",
            )
        return response

    @app.get("/go/winline/{event_id}")
    def go(event_id: str, request: Request, source: str = "stream"):
        if source not in ("stream", "legacy"):
            raise HTTPException(422, "Неизвестный источник.")
        destination = outbound(repo, event_id, source)
        accounts.limited(repo, "outbound:" + accounts.peer(request), 120, 60)
        if analytics_allowed(repo,request):
            user = accounts.current_user(request, False)
            record(repo,"outbound_click",event_id,user["id"] if user else None,visitor(request))
        response = RedirectResponse(destination, status_code=302)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/account/promo")
    async def redeem(request: Request):
        user = accounts.current_user(request)
        accounts.mutation(request)
        accounts.limited(repo, "promo:" + user["id"], 10, 3600)
        data = await accounts.body(request)
        code = text(data.get("code"), 40).upper()
        stamp = accounts.now()
        with repo.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            promo = db.execute(
                "SELECT * FROM promo_codes WHERE code=? AND status='active' AND starts_at<=? AND ends_at>?",
                (code, stamp, stamp),
            ).fetchone()
            if not promo:
                raise HTTPException(
                    422, "Промокод недоступен или срок его действия закончился."
                )
            if db.execute(
                "SELECT 1 FROM promo_redemptions WHERE promo_id=? AND user_id=?",
                (promo["id"], user["id"]),
            ).fetchone():
                raise HTTPException(409, "Вы уже активировали этот промокод.")
            uses = db.execute(
                "SELECT count(*) FROM promo_redemptions WHERE promo_id=?",
                (promo["id"],),
            ).fetchone()[0]
            if uses >= promo["max_uses"]:
                raise HTTPException(409, "Лимит активаций промокода исчерпан.")
            db.execute(
                "INSERT INTO promo_redemptions VALUES (?,?,?,?)",
                (promo["id"], user["id"], stamp, promo["bonus"]),
            )
            db.execute(
                "UPDATE users SET bonus_analyses=bonus_analyses+? WHERE id=?",
                (promo["bonus"], user["id"]),
            )
            accounts.audit(
                db, user["id"], "promo.redeemed", promo["id"], {"bonus": promo["bonus"]}
            )
        return {"ok": True, "bonus": promo["bonus"]}

    @app.get("/api/account/usage")
    def account_usage(request: Request):
        user = accounts.current_user(request)
        with repo.connection() as db:
            today = db.execute(
                "SELECT count(*) FROM ai_usage WHERE user_id=? AND status='success' AND at>=?",
                (user["id"], datetime.now(UTC).date().isoformat()),
            ).fetchone()[0]
            subscriptions = db.execute(
                "SELECT count(*) FROM push_subscriptions s WHERE s.user_id=? AND s.enabled=1 AND EXISTS (SELECT 1 FROM push_ad_consents a WHERE a.subscription_id=s.id AND a.user_id=s.user_id AND a.revoked_at IS NULL)",
                (user["id"],),
            ).fetchone()[0]
        return {
            "today": today,
            "daily_limit": settings.ai_daily_limit,
            "bonus_analyses": user["bonus_analyses"],
            "push_devices": subscriptions,
        }

    @app.post("/api/admin/reauth")
    async def reauth(request: Request):
        user = accounts.admin_user(request)
        accounts.mutation(request)
        accounts.limited(repo, "admin-password:" + user["id"], 5, 900)
        data = await accounts.body(request)
        password = data.get("password")
        from starlette.concurrency import run_in_threadpool

        valid = (
            isinstance(password, str)
            and len(password) <= 128
            and await run_in_threadpool(
                accounts.password_matches, password, user["password_hash"]
            )
        )
        with repo.connection() as db:
            accounts.audit(
                db,
                user["id"],
                "admin.reauth" if valid else "admin.reauth_failed",
                user["id"],
            )
            if valid:
                db.execute(
                    "UPDATE user_sessions SET elevated_at=? WHERE token_hash=?",
                    (accounts.now(), user["session_hash"]),
                )
        if not valid:
            raise HTTPException(401, "Неверный пароль администратора.")
        return {"ok": True, "valid_seconds": 600}

    @app.get("/api/admin/overview")
    def admin_overview(request: Request, days: int = 30):
        accounts.admin_user(request)
        if days not in (7, 30, 90):
            raise HTTPException(422, "Доступны периоды 7, 30 или 90 дней.")
        return overview(repo, days)

    @app.get("/api/admin/users")
    def users(request: Request, search: str = "", offset: int = 0):
        accounts.admin_user(request)
        if len(search) > 50 or offset < 0:
            raise HTTPException(422, "Некорректная страница.")
        args = ("%" + search.casefold() + "%",)
        with repo.connection() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT u.id,u.username,u.role,u.enabled,u.created_at,u.last_login_at,u.bonus_analyses,(SELECT count(*) FROM ai_usage a WHERE a.user_id=u.id AND a.status='success') analyses FROM users u WHERE username LIKE ? ORDER BY created_at DESC LIMIT 30 OFFSET ?",
                    (*args, offset),
                )
            ]
            total = db.execute(
                "SELECT count(*) FROM users WHERE username LIKE ?", args
            ).fetchone()[0]
        return {"users": rows, "total": total, "offset": offset}

    @app.patch("/api/admin/users/{user_id}")
    async def update_user(user_id: str, request: Request):
        actor = accounts.admin_user(request, True)
        accounts.mutation(request)
        data = await accounts.body(request)
        if set(data) != {"enabled"} or type(data["enabled"]) is not bool:
            raise HTTPException(422, "Разрешено изменить только состояние аккаунта.")
        with repo.connection() as db:
            row = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Пользователь не найден.")
            if row["role"] == "admin":
                raise HTTPException(
                    403,
                    "Управление администраторами доступно только локальной командой на сервере.",
                )
            db.execute(
                "UPDATE users SET enabled=? WHERE id=?", (int(data["enabled"]), user_id)
            )
            if not data["enabled"]:
                db.execute("DELETE FROM user_sessions WHERE user_id=?", (user_id,))
            accounts.audit(
                db,
                actor["id"],
                "user.enabled" if data["enabled"] else "user.disabled",
                user_id,
            )
        return {"ok": True}

    @app.get("/api/admin/promos")
    def promos(request: Request):
        accounts.admin_user(request)
        with repo.connection() as db:
            return {
                "promos": [
                    dict(r)
                    for r in db.execute(
                        "SELECT p.*,(SELECT count(*) FROM promo_redemptions r WHERE r.promo_id=p.id) uses FROM promo_codes p ORDER BY created_at DESC LIMIT 100"
                    )
                ]
            }

    @app.post("/api/admin/promos")
    async def create_promo(request: Request):
        actor = accounts.admin_user(request, True)
        accounts.mutation(request)
        data = await accounts.body(request)
        code = text(data.get("code"), 40).upper()
        if not re.fullmatch(r"[A-Z0-9_-]{4,40}", code):
            raise HTTPException(
                422, "Код: 4–40 латинских букв, цифр, дефисов или подчёркиваний."
            )
        title = text(data.get("title"), 100)
        bonus = integer(data.get("bonus"), 1, 1000)
        uses = integer(data.get("max_uses"), 1, 100000)
        start = date_time(data.get("starts_at"))
        end = date_time(data.get("ends_at"))
        if end <= start:
            raise HTTPException(422, "Окончание должно быть позже начала.")
        promo_id = uuid4().hex
        import sqlite3

        try:
            with repo.connection() as db:
                db.execute(
                    "INSERT INTO promo_codes VALUES (?,?,?,?,?,?,?,'draft',?,?)",
                    (
                        promo_id,
                        code,
                        title,
                        bonus,
                        uses,
                        start,
                        end,
                        accounts.now(),
                        actor["id"],
                    ),
                )
                accounts.audit(
                    db,
                    actor["id"],
                    "promo.created",
                    promo_id,
                    {"code": code, "bonus": bonus, "max_uses": uses},
                )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Промокод уже существует.") from exc
        return {"id": promo_id, "status": "draft"}

    @app.patch("/api/admin/promos/{promo_id}")
    async def update_promo(promo_id: str, request: Request):
        actor = accounts.admin_user(request, True)
        accounts.mutation(request)
        data = await accounts.body(request)
        status = data.get("status")
        if set(data) != {"status"} or status not in ("active", "paused", "draft"):
            raise HTTPException(422, "Неизвестное состояние промокода.")
        with repo.connection() as db:
            result = db.execute(
                "UPDATE promo_codes SET status=? WHERE id=?", (status, promo_id)
            )
            if not result.rowcount:
                raise HTTPException(404, "Промокод не найден.")
            accounts.audit(db, actor["id"], "promo." + status, promo_id)
        return {"ok": True}

    @app.get("/api/admin/links")
    def links(request: Request):
        accounts.admin_user(request)
        return {
            "template": setting(repo, "winline_referral"),
            "example": "https://winline.ru/stavki/event/12345",
            "placeholders": ["{url}", "{event_id}"],
        }

    @app.put("/api/admin/links")
    async def save_links(request: Request):
        actor = accounts.admin_user(request, True)
        accounts.mutation(request)
        data = await accounts.body(request)
        value = referral_template(data.get("template"))
        with repo.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO site_settings VALUES (?,?,?)",
                ("winline_referral", value, accounts.now()),
            )
            accounts.audit(
                db, actor["id"], "links.updated", "winline", {"enabled": bool(value)}
            )
        return {"ok": True}

    @app.get("/api/admin/audit")
    def audit_log(request: Request, offset: int = 0):
        accounts.admin_user(request)
        if offset < 0:
            raise HTTPException(422, "Некорректная страница.")
        with repo.connection() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT a.id,a.at,u.username,a.action,a.target,a.details FROM admin_audit a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT 50 OFFSET ?",
                    (offset,),
                )
            ]
            total = db.execute("SELECT count(*) FROM admin_audit").fetchone()[0]
        return {"events": rows, "offset": offset, "total": total}
