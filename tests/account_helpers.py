"""Real revocable sessions for existing business-logic fixtures (no auth bypass)."""

from functools import lru_cache

from matchlab import accounts


@lru_cache(maxsize=1)
def fixture_hash():
    return accounts.password_hash("fixture-password-long-enough")


def signed_in(client, repo, username="fixture_user", role="user"):
    with repo.connection() as db:
        db.execute(
            "INSERT OR IGNORE INTO users(id,username,password_hash,role,created_at) VALUES (?,?,?,?,?)",
            (username, username, fixture_hash(), role, accounts.now()),
        )
        accounts.save_agreements(db,username)
    token = accounts.make_session(repo, username)
    client.cookies.set("prognozich_session", token)
    client.headers["X-CSRF-Token"] = accounts.csrf(token)
    return username


async def signed_in_browser(page, base):
    from uuid import uuid4

    result = await page.context.request.post(
        base + "/api/auth/register",
        data={
            "username": "browser_" + uuid4().hex[:16],
            "password": "browser-fixture-password-123",
            "terms_accepted": True, "personal_data_consent": True, "age_confirmed": True, "legal_version": accounts.LEGAL_VERSION,
        },
    )
    assert result.status == 201, await result.text()
