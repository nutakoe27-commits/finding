"""Run on the site VPS: exercise both WebSocket protocols with synthetic speech."""
import asyncio
import io
import json
import math
import wave

import numpy as np
from dotenv import load_dotenv
from scipy.signal import resample_poly

from matchlab import companion_voice as voice


async def check():
    report = {}
    try:
        wav = await asyncio.wait_for(voice.russian_audio("Привет, Мира. Как дела?"), 10)
        with wave.open(io.BytesIO(wav)) as source:
            if source.getsampwidth() != 2 or source.getnchannels() != 1:
                raise ValueError("Unexpected audio format")
            rate = source.getframerate()
            samples = np.frombuffer(source.readframes(rate * 20), dtype="<i2")
        report["russian_voice"] = "ok"
        factor = math.gcd(rate, voice.SAMPLE_RATE)
        signal = resample_poly(samples.astype(float), voice.SAMPLE_RATE // factor, rate // factor)
        pcm = np.clip(signal, -32768, 32767).astype("<i2").tobytes()
        text = await asyncio.wait_for(voice.transcribe(pcm), 45)
        report["whisper_text"] = text
        report["whisper"] = "ok" if text else "empty"
    except Exception as exc:
        report["whisper_or_russian_voice_error"] = type(exc).__name__
    try:
        wav = await asyncio.wait_for(voice.kokoro("Hello, Mira."), 45)
        with wave.open(io.BytesIO(wav)) as source:
            report["kokoro"] = "ok" if source.getnframes() > 0 and source.getframerate() == 24000 else "empty"
    except Exception as exc:
        report["kokoro_error"] = type(exc).__name__
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report.get("whisper") == report.get("kokoro") == "ok"


if __name__ == "__main__":
    load_dotenv(".env")
    raise SystemExit(0 if asyncio.run(check()) else 1)
