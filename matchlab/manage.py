"""Local-only account recovery and Web Push key initialization."""

import argparse
import base64
import getpass
import os
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException

from . import accounts
from .config import ROOT, Settings
from .storage import Repository


def new_password():
    password = getpass.getpass("Пароль (не менее 12 символов): ")
    repeat = getpass.getpass("Повторите пароль: ")
    if password != repeat:
        raise ValueError("Пароли не совпадают.")
    return password


def write_env(path, updates):
    if path.is_symlink():
        raise ValueError("Файл .env не должен быть ссылкой.")
    previous = path.read_text() if path.exists() else ""
    lines = previous.splitlines()
    keys = set(updates)
    lines = [line for line in lines if line.split("=", 1)[0].strip() not in keys]
    lines.extend(f"{key}={value}" for key, value in updates.items())
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write("\n".join(lines) + "\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


def init_push(settings):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from .push import configured

    if configured(settings):
        print("Ключи Web Push уже настроены; существующие подписки сохранены.")
        return
    if any(
        (settings.vapid_private_key, settings.vapid_public_key, settings.vapid_subject)
    ):
        raise ValueError(
            "Настройка Web Push заполнена частично. Проверьте существующие WEBPUSH_* в .env; ключи автоматически не заменяются."
        )
    if not settings.public_origin.startswith("https://"):
        raise ValueError("Сначала задайте PUBLIC_ORIGIN=https://ваш-домен в .env.")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    path = settings.data_dir.resolve() / "webpush-private.pem"
    if path.is_symlink():
        raise ValueError("Путь ключа не должен быть ссылкой.")
    if path.exists():
        private = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(private, ec.EllipticCurvePrivateKey) or not isinstance(
            private.curve, ec.SECP256R1
        ):
            raise ValueError("Неожиданный существующий ключ.")
    else:
        private = ec.generate_private_key(ec.SECP256R1())
        data = private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    public = (
        base64.urlsafe_b64encode(
            private.public_key().public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
        )
        .rstrip(b"=")
        .decode()
    )
    write_env(
        ROOT / ".env",
        {
            "WEBPUSH_PRIVATE_KEY_FILE": str(path),
            "WEBPUSH_PUBLIC_KEY": public,
            "WEBPUSH_SUBJECT": settings.public_origin,
        },
    )
    print(
        "Ключи созданы. Закрытый ключ не выводится. Перезапустите приложение и установите службу очереди push."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("create-admin", "set-password", "revoke-sessions"):
        command = sub.add_parser(action)
        command.add_argument("username")
    sub.add_parser("list-admins")
    sub.add_parser("init-push")
    sub.add_parser("backup")
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.action == "backup":
        if not settings.database.is_file():
            parser.exit(1, "База ещё не создана.\n")
        if (
            shutil.disk_usage(settings.database.parent).free
            < settings.database.stat().st_size * 2 + 50 * 1024**2
        ):
            parser.exit(1, "Недостаточно места для резервной копии.\n")
        directory = settings.database.parent / "backups"
        directory.mkdir(mode=0o700, exist_ok=True)
        target = directory / (
            "matchlab-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite3"
        )
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        from urllib.parse import quote

        try:
            with (
                sqlite3.connect(
                    "file:" + quote(str(settings.database.resolve())) + "?mode=ro",
                    uri=True,
                ) as source,
                sqlite3.connect(target) as destination,
            ):
                source.backup(destination)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        print("Резервная копия: " + str(target))
        return
    repo = Repository(settings.database)
    repo.initialize()
    try:
        if args.action == "create-admin":
            user_id = accounts.create_user(
                repo, args.username, new_password(), role="admin"
            )
            print(
                f"Администратор {args.username.casefold()} создан ({user_id}). Войдите на сайте обычной формой, затем откройте #admin."
            )
        elif args.action == "init-push":
            init_push(settings)
        elif args.action == "list-admins":
            with repo.connection() as db:
                for row in db.execute(
                    "SELECT username,enabled,created_at FROM users WHERE role='admin'"
                ):
                    print(dict(row))
        else:
            name = args.username.casefold()
            with repo.connection() as db:
                row = db.execute(
                    "SELECT * FROM users WHERE username=?", (name,)
                ).fetchone()
            if not row:
                raise ValueError("Аккаунт не найден.")
            encoded = None
            if args.action == "set-password":
                password = new_password()
                accounts.validate_credentials(name, password)
                encoded = accounts.password_hash(password)
            with repo.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                if encoded:
                    db.execute(
                        "UPDATE users SET password_hash=? WHERE id=?",
                        (encoded, row["id"]),
                    )
                db.execute("DELETE FROM user_sessions WHERE user_id=?", (row["id"],))
                accounts.audit(
                    db,
                    None,
                    "account.local_password_reset"
                    if encoded
                    else "account.sessions_revoked",
                    row["id"],
                )
            print("Готово. Прежние сеансы завершены.")
    except (HTTPException, ValueError) as exc:
        parser.exit(
            1, str(exc.detail if isinstance(exc, HTTPException) else exc) + "\n"
        )


if __name__ == "__main__":
    main()
