"""Opt-in Web Push and a leased, retryable delivery queue. No browser polling sender."""

import argparse
import base64
import json
import signal
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import HTTPException, Request

from . import accounts, growth

STOP = False
SEGMENTS = {"all", "active_7d", "self"}


def configured(settings):
    return bool(
        settings.vapid_public_key
        and settings.vapid_private_key
        and settings.vapid_subject
        and Path(settings.vapid_private_key).is_file()
    )


def subscription(value):
    if not isinstance(value, dict):
        raise HTTPException(422, "Некорректная подписка.")
    endpoint = value.get("endpoint", "")
    if not isinstance(endpoint, str):
        raise HTTPException(422, "Некорректный адрес push-службы.")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(422, "Некорректный адрес push-службы.") from exc
    hosts = (
        "fcm.googleapis.com",
        "updates.push.services.mozilla.com",
        "web.push.apple.com",
    )
    host = parsed.hostname or ""
    valid = host in hosts or host.endswith(
        (".push.services.mozilla.com", ".notify.windows.com")
    )
    if (
        not valid
        or parsed.scheme != "https"
        or port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.fragment
        or len(endpoint) > 3000
    ):
        raise HTTPException(422, "Неподдерживаемый адрес push-службы.")
    keys = value.get("keys") or {}
    try:

        def decode(name, size):
            item = keys[name]
            if not isinstance(item, str) or len(item) > 120:
                raise ValueError("key")
            raw = base64.urlsafe_b64decode(item + "=" * (-len(item) % 4))
            if len(raw) != size:
                raise ValueError("key")
            return raw

        public = decode("p256dh", 65)
        decode("auth", 16)
        from cryptography.hazmat.primitives.asymmetric import ec

        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(422, "Некорректные ключи push-подписки.") from exc
    return endpoint, {"p256dh": keys["p256dh"], "auth": keys["auth"]}


def local_url(value):
    import re

    if not isinstance(value, str) or not re.fullmatch(
        r"/(?:#(?:matches|archive|account|(?:match|stream)/[0-9]{1,20}))?", value
    ):
        raise HTTPException(
            422, "Укажите страницу сервиса: /#matches, /#archive или ссылку на матч."
        )
    return value


def recipients(db, segment, actor):
    sql = "SELECT s.id FROM push_subscriptions s JOIN users u ON u.id=s.user_id WHERE s.enabled=1 AND u.enabled=1 AND EXISTS (SELECT 1 FROM push_ad_consents a WHERE a.subscription_id=s.id AND a.user_id=s.user_id AND a.revoked_at IS NULL)"
    args = []
    if segment == "self":
        sql += " AND u.id=?"
        args.append(actor)
    if segment == "active_7d":
        sql += " AND u.last_login_at>=?"
        args.append(
            (datetime.now(UTC) - timedelta(days=7)).isoformat(timespec="microseconds")
        )
    return [r["id"] for r in db.execute(sql, args)]


def campaigns(repo):
    with repo.connection() as db:
        return [
            dict(r)
            for r in db.execute("""SELECT c.*,
        (SELECT count(*) FROM push_outbox o WHERE o.campaign_id=c.id) audience,
        (SELECT count(*) FROM push_outbox o WHERE o.campaign_id=c.id AND status='sent') sent,
        (SELECT count(*) FROM push_outbox o WHERE o.campaign_id=c.id AND status IN ('failed','expired')) failed,
        (SELECT count(*) FROM push_outbox o WHERE o.campaign_id=c.id AND status IN ('pending','retry','inflight')) pending
        FROM push_campaigns c ORDER BY created_at DESC LIMIT 100""")
        ]


def install(app, repo, settings):
    @app.get("/api/push/config")
    def config():
        return {
            "enabled": configured(settings),
            "public_key": settings.vapid_public_key if configured(settings) else None,
        }

    @app.post("/api/push/subscribe")
    async def subscribe(request: Request):
        user = accounts.current_user(request)
        accounts.mutation(request)
        accounts.limited(repo, "push-sub:" + user["id"], 20, 3600)
        if not configured(settings):
            raise HTTPException(503, "Уведомления ещё не настроены.")
        data=await accounts.body(request)
        if data.get('advertising_consent') is not True or data.get('consent_version')!=accounts.LEGAL_VERSION:
            raise HTTPException(422,'Для подписки подтвердите отдельное согласие на push-уведомления.')
        endpoint, keys = subscription(data)
        sub_id = accounts.token_hash(endpoint)
        with repo.connection() as db:
            # A browser subscription belongs to its currently signed-in owner.
            db.execute(
                "INSERT INTO push_subscriptions VALUES (?,?,?,?,?,1,NULL) ON CONFLICT(id) DO UPDATE SET user_id=excluded.user_id,keys_json=excluded.keys_json,enabled=1,last_status=NULL",
                (sub_id, user["id"], endpoint, json.dumps(keys), accounts.now()),
            )
            db.execute("INSERT INTO push_ad_consents VALUES (?,?,?,?,NULL) ON CONFLICT(subscription_id,user_id) DO UPDATE SET version=excluded.version,accepted_at=excluded.accepted_at,revoked_at=NULL",
                       (sub_id,user['id'],accounts.LEGAL_VERSION,accounts.now()))
        return {"ok": True}

    @app.post("/api/push/unsubscribe")
    async def unsubscribe(request: Request):
        user = accounts.current_user(request)
        accounts.mutation(request)
        data = await accounts.body(request)
        endpoint = growth.text(data.get("endpoint"), 3000)
        with repo.connection() as db:
            db.execute(
                "UPDATE push_subscriptions SET enabled=0 WHERE id=? AND user_id=?",
                (accounts.token_hash(endpoint), user["id"]),
            )
            db.execute("UPDATE push_ad_consents SET revoked_at=? WHERE subscription_id=? AND user_id=? AND revoked_at IS NULL",
                       (accounts.now(),accounts.token_hash(endpoint),user['id']))
        return {"ok": True}

    @app.post('/api/push/unsubscribe-all')
    def unsubscribe_all(request: Request):
        user=accounts.current_user(request)
        accounts.mutation(request)
        with repo.connection() as db:
            db.execute('UPDATE push_subscriptions SET enabled=0 WHERE user_id=?',(user['id'],))
            db.execute('UPDATE push_ad_consents SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL',(accounts.now(),user['id']))
        return {'ok':True}

    @app.get("/api/admin/push")
    def listing(request: Request):
        actor = accounts.admin_user(request)
        with repo.connection() as db:
            counts = {name: len(recipients(db, name, actor["id"])) for name in SEGMENTS}
            row = db.execute("SELECT * FROM push_worker_status WHERE id=1").fetchone()
        worker = dict(row) if row else None
        recent = bool(
            worker
            and worker["state"] == "running"
            and datetime.fromisoformat(worker["at"])
            > datetime.now(UTC) - timedelta(seconds=90)
        )
        return {
            "enabled": configured(settings),
            "worker": worker,
            "worker_recent": recent,
            "audiences": counts,
            "campaigns": campaigns(repo),
        }

    @app.post("/api/admin/push")
    async def draft(request: Request):
        actor = accounts.admin_user(request, True)
        accounts.mutation(request)
        data = await accounts.body(request)
        title = growth.text(data.get("title"), 80)
        message = growth.text(data.get("body"), 240)
        url = local_url(data.get("url"))
        segment = data.get("segment")
        if segment not in SEGMENTS:
            raise HTTPException(422, "Неизвестная аудитория.")
        scheduled = (
            growth.date_time(data["scheduled_at"])
            if data.get("scheduled_at")
            else accounts.now()
        )
        campaign_id = uuid4().hex
        with repo.connection() as db:
            db.execute(
                "INSERT INTO push_campaigns VALUES (?,?,?,?,?,'draft',?,?,?)",
                (
                    campaign_id,
                    title,
                    message,
                    url,
                    segment,
                    scheduled,
                    accounts.now(),
                    actor["id"],
                ),
            )
            accounts.audit(
                db, actor["id"], "push.draft", campaign_id, {"segment": segment}
            )
        return {"id": campaign_id, "status": "draft"}

    @app.get("/api/admin/push/{campaign_id}/preview")
    def preview(campaign_id: str, request: Request):
        actor = accounts.admin_user(request)
        with repo.connection() as db:
            row = db.execute(
                "SELECT * FROM push_campaigns WHERE id=?", (campaign_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "Кампания не найдена.")
            count = len(recipients(db, row["segment"], actor["id"]))
        return {
            "campaign": dict(row),
            "audience": count,
            "enabled": configured(settings),
        }

    @app.post("/api/admin/push/{campaign_id}/send")
    async def enqueue(campaign_id: str, request: Request):
        actor = accounts.admin_user(request, True)
        accounts.mutation(request)
        data = await accounts.body(request)
        if not configured(settings):
            raise HTTPException(409, "Сначала настройте ключи Web Push на VPS.")
        with repo.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM push_campaigns WHERE id=?", (campaign_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "Кампания не найдена.")
            if row["status"] != "draft":
                raise HTTPException(409, "Эта кампания уже отправлена или отменена.")
            ids = recipients(db, row["segment"], actor["id"])
            if type(data.get("confirm_audience")) is not int or data[
                "confirm_audience"
            ] != len(ids):
                raise HTTPException(
                    409,
                    "Аудитория изменилась. Откройте предпросмотр и подтвердите отправку заново.",
                )
            if not ids:
                raise HTTPException(409, "В выбранной аудитории пока нет подписок.")
            db.executemany(
                "INSERT INTO push_outbox(campaign_id,subscription_id,status,next_at) VALUES (?,?,'pending',?)",
                [(campaign_id, i, row["scheduled_at"]) for i in ids],
            )
            db.execute(
                "UPDATE push_campaigns SET status='queued' WHERE id=?", (campaign_id,)
            )
            accounts.audit(
                db,
                actor["id"],
                "push.queued",
                campaign_id,
                {"subscriptions": len(ids), "scheduled_at": row["scheduled_at"]},
            )
        return {"queued": len(ids)}

    @app.post("/api/admin/push/{campaign_id}/cancel")
    def cancel(campaign_id: str, request: Request):
        actor = accounts.admin_user(request, True)
        accounts.mutation(request)
        with repo.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status FROM push_campaigns WHERE id=?", (campaign_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "Кампания не найдена.")
            if row["status"] in ("complete", "cancelled"):
                raise HTTPException(409, "Кампания уже завершена.")
            db.execute(
                "UPDATE push_campaigns SET status='cancelled' WHERE id=?",
                (campaign_id,),
            )
            db.execute(
                "UPDATE push_outbox SET status='cancelled' WHERE campaign_id=? AND status IN ('pending','retry')",
                (campaign_id,),
            )
            accounts.audit(db, actor["id"], "push.cancelled", campaign_id)
        return {"ok": True}


def claim(repo):
    stamp = accounts.now()
    with repo.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            """SELECT o.*,s.endpoint,s.keys_json,s.user_id,c.title,c.body,c.url FROM push_outbox o
        JOIN push_subscriptions s ON s.id=o.subscription_id JOIN push_campaigns c ON c.id=o.campaign_id
        WHERE c.status IN ('queued','sending') AND ((o.status IN ('pending','retry') AND o.next_at<=?)
        OR (o.status='inflight' AND o.lease_until<?)) ORDER BY o.next_at LIMIT 1""",
            (stamp, stamp),
        ).fetchone()
        if not row:
            return None
        if row["attempts"] >= 5:
            db.execute(
                "UPDATE push_outbox SET status='failed',lease_until=NULL,last_status='attempt_limit' WHERE campaign_id=? AND subscription_id=?",
                (row["campaign_id"], row["subscription_id"]),
            )
            pending = db.execute(
                "SELECT 1 FROM push_outbox WHERE campaign_id=? AND status IN ('pending','retry','inflight') LIMIT 1",
                (row["campaign_id"],),
            ).fetchone()
            if not pending:
                db.execute(
                    "UPDATE push_campaigns SET status='complete' WHERE id=?",
                    (row["campaign_id"],),
                )
            return None
        result = dict(row)
        lease_id = uuid4().hex
        db.execute(
            "UPDATE push_outbox SET status='inflight',attempts=attempts+1,lease_until=?,lease_id=? WHERE campaign_id=? AND subscription_id=?",
            (
                (datetime.now(UTC) + timedelta(minutes=2)).isoformat(
                    timespec="microseconds"
                ),
                lease_id,
                row["campaign_id"],
                row["subscription_id"],
            ),
        )
        db.execute(
            "UPDATE push_campaigns SET status='sending' WHERE id=?",
            (row["campaign_id"],),
        )
        result["lease_id"] = lease_id
        result["attempts"] += 1
        return result


def deliver(settings, row):
    import requests
    from pywebpush import WebPushException, webpush

    class NoRedirectSession(requests.Session):
        def request(self, *args, **kwargs):
            kwargs["allow_redirects"] = False
            return super().request(*args, **kwargs)

    try:
        endpoint, keys = subscription(
            {"endpoint": row["endpoint"], "keys": json.loads(row["keys_json"])}
        )
        with NoRedirectSession() as client:
            response = webpush(
                subscription_info={"endpoint": endpoint, "keys": keys},
                data=json.dumps(
                    {
                        "title": row["title"],
                        "body": row["body"],
                        "url": row["url"],
                        "campaign_id": row["campaign_id"],
                    },
                    ensure_ascii=False,
                ),
                vapid_private_key=settings.vapid_private_key,
                vapid_claims={"sub": settings.vapid_subject},
                ttl=3600,
                timeout=15,
                requests_session=client,
            )
        return response.status_code
    except WebPushException as exc:
        return exc.response.status_code if exc.response is not None else 503
    except (requests.RequestException, OSError, ValueError, HTTPException):
        return 503


def work_once(repo, settings, sender=deliver):
    if not configured(settings):
        return False
    row = claim(repo)
    if not row:
        return False
    with repo.connection() as db:
        eligible = db.execute(
            "SELECT 1 FROM push_subscriptions s JOIN users u ON u.id=s.user_id JOIN push_campaigns c ON c.id=? WHERE s.id=? AND s.user_id=? AND s.enabled=1 AND u.enabled=1 AND c.status IN ('queued','sending') AND EXISTS (SELECT 1 FROM push_ad_consents a WHERE a.subscription_id=s.id AND a.user_id=s.user_id AND a.revoked_at IS NULL)",
            (row["campaign_id"], row["subscription_id"], row["user_id"]),
        ).fetchone()
    status = sender(settings, row) if eligible else 410
    state = (
        "sent"
        if 200 <= status < 300
        else "expired"
        if status in (404, 410)
        else "retry"
        if (status == 429 or status >= 500) and row["attempts"] < 5
        else "failed"
    )
    with repo.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        lease = db.execute(
            "SELECT lease_id,status FROM push_outbox WHERE campaign_id=? AND subscription_id=?",
            (row["campaign_id"], row["subscription_id"]),
        ).fetchone()
        if (
            not lease
            or lease["lease_id"] != row["lease_id"]
            or lease["status"] != "inflight"
        ):
            return True
        db.execute(
            "UPDATE push_outbox SET status=?,next_at=?,lease_until=NULL,last_status=?,sent_at=? WHERE campaign_id=? AND subscription_id=?",
            (
                state,
                (
                    datetime.now(UTC) + timedelta(seconds=60 * 2 ** row["attempts"])
                ).isoformat(timespec="microseconds"),
                str(status),
                accounts.now() if state == "sent" else None,
                row["campaign_id"],
                row["subscription_id"],
            ),
        )
        if status in (404, 410) and eligible:
            db.execute(
                "UPDATE push_subscriptions SET enabled=0,last_status=? WHERE id=?",
                (str(status), row["subscription_id"]),
            )
        remaining = db.execute(
            "SELECT count(*) FROM push_outbox WHERE campaign_id=? AND status IN ('pending','retry','inflight')",
            (row["campaign_id"],),
        ).fetchone()[0]
        if not remaining:
            db.execute(
                "UPDATE push_campaigns SET status='complete' WHERE id=? AND status<>'cancelled'",
                (row["campaign_id"],),
            )
    return True


def stop(*_):
    global STOP
    STOP = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    from .config import Settings
    from .storage import Repository

    settings = Settings.from_env()
    repo = Repository(settings.database)
    repo.initialize()
    if not configured(settings):
        parser.exit(
            1,
            "Web Push keys are not configured. Run python -m matchlab.manage init-push.\n",
        )
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def heartbeat(state, error=None):
        with repo.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO push_worker_status VALUES (1,?,?,?)",
                (accounts.now(), state, error),
            )

    last_heartbeat = 0
    while not STOP:
        try:
            if time.monotonic() - last_heartbeat >= 5:
                heartbeat("running")
                last_heartbeat = time.monotonic()
            worked = work_once(repo, settings)
        except Exception as exc:  # noqa: BLE001 -- isolate an unattended delivery failure; log only its type.
            heartbeat("error", type(exc).__name__)
            print(
                json.dumps({"state": "error", "error_type": type(exc).__name__}),
                flush=True,
            )
            worked = False
        if args.once:
            break
        if not worked:
            for _ in range(5):
                if STOP:
                    break
                time.sleep(1)
    heartbeat("stopped")


if __name__ == "__main__":
    main()
