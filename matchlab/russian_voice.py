"""Local Russian VITS2 voice. Weights stay outside Git and are installed once per VPS."""
import argparse
import hashlib
import io
import os
import re
import tempfile
import threading
import urllib.request
import wave
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

# Symbol IDs from symbols.py shipped with this specific Natasha checkpoint.
ALPHABET = "_ !+,-.:;?«»—абвгдежзийклмнопрстуфхцчшщъыьэюяё"
SYMBOLS = {symbol: i for i, symbol in enumerate(ALPHABET)}
REVISION = "ba8fe76135332065b855f7b0c99a1f1db2b06477"
DIGEST = "95e975a44d361f3d9aa66ed62c9d80efc996c4c453dfb3771cc3e7d7aabdb3a6"
URL = f"https://huggingface.co/frappuccino/vits2_ru_natasha/resolve/{REVISION}/natasha.onnx?download=true"
ROOT = Path(__file__).resolve().parents[1]
_lock = threading.Lock()
_session = None


def model_path():
    return Path(os.getenv("MIRA_RU_MODEL") or ROOT / ".local/voices/natasha.onnx")


def install_model():
    dest = model_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file():
        with dest.open("rb") as src:
            if hashlib.file_digest(src, "sha256").hexdigest() == DIGEST:
                return
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        checksum = hashlib.sha256()
        with urllib.request.urlopen(URL, timeout=60) as response, tmp_path.open("wb") as out:
            while chunk := response.read(1024 * 1024):
                checksum.update(chunk)
                out.write(chunk)
        if checksum.hexdigest() != DIGEST:
            raise ValueError("Контрольная сумма модели не совпадает.")
        tmp_path.replace(dest)
    finally:
        tmp_path.unlink(missing_ok=True)


def spoken_chunks(text):
    from num2words import num2words
    text = text.lower().replace("…", ".")
    text = re.sub(r"\d{1,15}", lambda m: num2words(int(m.group()), lang="ru"), text)
    text = text.replace("%", " процентов ").replace("₽", " рублей ")
    # Expand frequent foreign abbreviations in otherwise Russian utterances.
    text = re.sub(r"\bwi-fi\b", "вай фай", text)
    names = {"ai": "эй ай", "gpt": "джи пи ти", "chatgpt": "чат джи пи ти",
             "telegram": "телеграм", "youtube": "ютуб", "zoom": "зум", "ok": "окей"}
    letters = dict(zip("abcdefghijklmnopqrstuvwxyz", (
        "эй", "би", "си", "ди", "и", "эф", "джи", "эйч", "ай", "джей", "кей", "эл", "эм",
        "эн", "оу", "пи", "кью", "ар", "эс", "ти", "ю", "ви", "дабл ю", "икс", "уай", "зед")))
    text = re.sub(r"[a-z]+", lambda m: names.get(m[0]) or " ".join(letters[c] for c in m[0]), text)
    text = re.sub(r"\s*\n+\s*", ". ", text)
    text = re.sub(r"[^а-яё !+,.:;?«»—-]+", " ", text)
    for sentence in re.split(r"(?<=[.!?;])\s+", text):
        chunk = ""
        for piece in re.findall(r"\S{1,180}", sentence):
            if chunk and len(chunk) + 1 + len(piece) > 180:
                yield chunk
                chunk = ""
            chunk = (chunk + " " + piece).strip()
        if chunk:
            yield chunk


def synthesize(text):
    import onnxruntime as ort
    global _session
    # Keep one warm CPU session. Serialising inference also bounds transient RAM.
    with _lock:
        if _session is None:
            if not model_path().is_file():
                raise FileNotFoundError("Установите русскую модель голоса Миры.")
            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            options.inter_op_num_threads = 1
            _session = ort.InferenceSession(str(model_path()), options, providers=["CPUExecutionProvider"])
        buf = io.BytesIO()
        with wave.open(buf, "wb") as out:
            out.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            for phrase in spoken_chunks(text):
                if not re.search(r"[а-яё]", phrase):
                    continue
                ids = np.array([[SYMBOLS[ch] for ch in phrase]], dtype=np.int64)
                audio = _session.run(None, {"input": ids, "input_lengths": np.array([ids.size], dtype=np.int64),
                                           "scales": np.array([0.667, 1.0, 0.8], dtype=np.float32)})[0]
                out.writeframes((np.clip(audio.ravel(), -1, 1) * 32767).astype("<i2").tobytes())
                out.writeframes(bytes(2205 * 2))
            if not out.getnframes():
                raise ValueError("Нет текста для русской озвучки.")
        return buf.getvalue()


if __name__ == "__main__":
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", action="store_true", required=True)
    parser.parse_args()
    install_model()
    synthesize("Привет, я Мира.")
    print("Русский голос Миры готов.")
