"""Mira's local Russian voice (Supertonic 3, preset F2)."""
import argparse
import hashlib
import io
import os
import re
import shutil
import tarfile
import tempfile
import threading
import urllib.request
import wave
from functools import lru_cache
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from num2words import num2words

ROOT = Path(__file__).resolve().parents[1]
REVISION = "3cadd1ee6394adea1bd021217a0e650ede09a323"
BASE_URL = f"https://huggingface.co/Supertone/supertonic-3/resolve/{REVISION}"
_lock = threading.Lock()
FILES = {
    "LICENSE": "0d944a9110fed9a9602d60e0423a272903e7bd21ab060490774efc77c2275e9f",
    "config.json": "4099082b107a9d4029849ac76b89eca65e03732660969c2babe5bf308c7357f2",
    "onnx/duration_predictor.onnx": "c3eb91414d5ff8a7a239b7fe9e34e7e2bf8a8140d8375ffb14718b1c639325db",
    "onnx/text_encoder.onnx": "c7befd5ea8c3119769e8a6c1486c4edc6a3bc8365c67621c881bbb774b9902ff",
    "onnx/tts.json": "42078d3aef1cd43ab43021f3c54f47d2d75ceb4e75f627f118890128b06a0d09",
    "onnx/unicode_indexer.json": "9bf7346e43883a81f8645c81224f786d43c5b57f3641f6e7671a7d6c493cb24f",
    "onnx/vector_estimator.onnx": "883ac868ea0275ef0e991524dc64f16b3c0376efd7c320af6b53f5b780d7c61c",
    "onnx/vocoder.onnx": "085de76dd8e8d5836d6ca66826601f615939218f90e519f70ee8a36ed2a4c4ba",
    "voice_styles/F2.json": "7c722c6a72707b1a77f035d67f0d1351ba187738e06f7683e8c72b1df3477fc6",
}


def model_dir():
    return Path(os.getenv("MIRA_SUPERTONIC_DIR") or ROOT / ".local/voices/supertonic-3-f2")


def verify(directory):
    for name, expected in FILES.items():
        with (directory / name).open("rb") as data:
            if hashlib.file_digest(data, "sha256").hexdigest() != expected:
                raise ValueError(f"Не совпадает SHA-256 файла {name}")


def install_model(archive=None):
    dest = model_dir()
    if dest.exists():
        verify(dest)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        stage = Path(tmp) / "model"
        stage.mkdir()
        if archive:
            with tarfile.open(archive, "r|gz") as bundle:
                for entry in bundle:
                    if entry.name in FILES and entry.isfile():
                        target = stage / entry.name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with bundle.extractfile(entry) as src, target.open("xb") as out:
                            shutil.copyfileobj(src, out)
        else:
            for name in FILES:
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with urllib.request.urlopen(f"{BASE_URL}/{name}?download=true", timeout=90) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
        verify(stage)
        stage.rename(dest)


@lru_cache(maxsize=1)
def load_voice():
    from supertonic import TTS

    if not (model_dir() / "voice_styles/F2.json").is_file():
        raise FileNotFoundError("Установите модель командой python -m matchlab.russian_voice --install")
    engine = TTS(model="supertonic-3", model_dir=str(model_dir()), auto_download=False,
                 intra_op_num_threads=2, inter_op_num_threads=1)
    return engine, engine.get_voice_style("F2")


def synthesize(text):
    text = re.sub(r"\b\d{1,15}\b", lambda m: num2words(int(m[0]), lang="ru"), text)
    with _lock:
        engine, style = load_voice()
        _, unsupported = engine.model.text_processor.validate_text(text)
        text = text.translate(dict.fromkeys(map(ord, unsupported), " "))
        if not any(ch.isalnum() for ch in text):
            raise ValueError("Нет текста для озвучки.")
        samples, _ = engine.synthesize(text, voice_style=style, lang="ru", total_steps=8, speed=1.0)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setparams((1, 2, engine.sample_rate, 0, "NONE", "not compressed"))
        wav.writeframes((np.clip(samples.ravel(), -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


if __name__ == "__main__":
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", action="store_true", required=True)
    parser.add_argument("--archive", type=Path, help="Локальный архив модели вместо загрузки")
    args = parser.parse_args()
    install_model(args.archive)
    synthesize("Алло? Привет! Да, я тебя слушаю.")
    print("Голос А готов: Supertonic 3, F2, русский, 44100 Гц.")
