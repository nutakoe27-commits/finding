"""Prepare a private worker package on the site VPS. Never print the bearer key."""
import os
import secrets
import tarfile
import tempfile
from pathlib import Path

from matchlab.config import ROOT, Settings


def main():
    state = Settings.from_env().data_dir
    state.mkdir(parents=True, exist_ok=True)
    key = state / "photo-worker.key"
    if not key.exists():
        with tempfile.NamedTemporaryFile(dir=state, delete=False) as f:
            temp = Path(f.name)
            f.write(secrets.token_hex(32).encode())
        try:
            os.link(temp, key)
        except FileExistsError:
            pass
        finally:
            temp.unlink()
    if len(key.read_text().strip()) < 32:
        raise ValueError("Invalid existing photo-worker.key")
    with tempfile.NamedTemporaryFile(dir=state, delete=False) as f:
        temp = Path(f.name)
    dest = state / "mira-photo-worker.tar.gz"
    try:
        with tarfile.open(temp, "w:gz") as package:
            for path in (key, ROOT / "matchlab/static/companion/mira.webp",
                         ROOT / "deploy/companion_photo/worker.py", ROOT / "deploy/companion_photo/mira-photo.service"):
                package.add(path, arcname=path.name)
        os.replace(temp, dest)
    finally:
        temp.unlink(missing_ok=True)
    print(dest)


if __name__ == "__main__":
    main()
