"""Companion activity metadata and role-checked conversation review."""
from pathlib import Path

from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse

from . import accounts


def visible_turn(turn):
    visible = {key: value for key, value in turn.items() if key != "queued"}
    if turn.get("queued"):
        visible["has_more"] = True
    return visible


def record(db, user_id, action):
    db.execute("INSERT INTO companion_activity(user_id,at,action) VALUES (?,?,?)",
               (user_id, accounts.now(), action))
    cutoff = db.execute("SELECT id FROM companion_activity WHERE user_id=? ORDER BY id DESC LIMIT 1 OFFSET 100",
                        (user_id,)).fetchone()
    if cutoff:
        db.execute("DELETE FROM companion_activity WHERE user_id=? AND id<=?", (user_id, cutoff[0]))


def install(app, repo):
    @app.get("/admin")
    def page(request: Request):
        accounts.admin_user(request)
        return FileResponse(Path(__file__).parent / "static/companion/admin.html",
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/admin/companion/users")
    def users(request: Request, search: str = Query("", max_length=80), offset: int = Query(0, ge=0)):
        accounts.admin_user(request)
        term = "%" + search.strip() + "%"
        with repo.connection() as db:
            rows = db.execute("""
                SELECT u.id,u.username,u.display_name,u.role,u.enabled,u.created_at,u.last_login_at,u.last_login_provider,
                  (SELECT a.at FROM companion_activity a WHERE a.user_id=u.id ORDER BY a.id DESC LIMIT 1) AS last_activity
                FROM users u WHERE u.username LIKE ? OR u.display_name LIKE ?
                ORDER BY u.created_at DESC,u.id LIMIT 31 OFFSET ?
            """, (term, term, offset)).fetchall()
        return {"users": [dict(row) for row in rows[:30]], "has_more": len(rows) > 30}

    @app.get("/api/admin/companion/users/{user_id}")
    def detail(user_id: str, request: Request):
        import json
        actor = accounts.admin_user(request)
        with repo.connection() as db:
            user = db.execute("""SELECT id,username,display_name,role,enabled,created_at,last_login_at,last_login_provider
                                 FROM users WHERE id=?""", (user_id,)).fetchone()
            if not user:
                raise HTTPException(404, "Пользователь не найден.")
            row = db.execute("SELECT turns FROM companion_dialogs WHERE user_id=?", (user_id,)).fetchone()
            turns = [visible_turn(t) for t in json.loads(row[0]) if t.get("shared_version")] if row else []
            events = db.execute("SELECT at,action FROM companion_activity WHERE user_id=? ORDER BY id DESC LIMIT 100",
                                (user_id,)).fetchall()
            accounts.audit(db, actor["id"], "companion.review", user_id)
        return {"user": dict(user), "turns": turns, "events": [dict(e) for e in events]}
