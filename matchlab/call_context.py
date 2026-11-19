"""Short-lived call context held by the client as an opaque, session-bound token."""
import json
import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException


def cipher(directory: Path):
    path = directory / "companion-call.key"
    if not path.exists():
        directory.mkdir(parents=True, exist_ok=True)
        # Publish only complete key material, including across app workers.
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as f:
            tmp = Path(f.name)
            f.write(Fernet.generate_key())
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
        finally:
            tmp.unlink()
    return Fernet(path.read_bytes())


def unpack(box, session_hash, token):
    if token is None:
        return []
    if not isinstance(token, str) or len(token) > 65000:
        raise HTTPException(422, "Некорректный контекст звонка.")
    try:
        data = json.loads(box.decrypt(token.encode(), ttl=1200))
    except (InvalidToken, ValueError):
        raise HTTPException(409, "Соединение завершилось. Перезвоните Мире.")
    if data["session"] != session_hash:
        raise HTTPException(403, "Этот звонок открыт в другой сессии.")
    return data["turns"]


def pack(box, session_hash, turns):
    data = {"session": session_hash, "turns": turns[-4:]}
    return box.encrypt(json.dumps(data, ensure_ascii=False).encode()).decode()
