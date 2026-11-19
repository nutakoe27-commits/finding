"""Short, authenticated voice notes over the existing Englishbot model tunnel."""
import asyncio
import base64
import binascii
import io
import json
import os
import re
import wave

from fastapi import HTTPException, Request, Response
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from starlette.concurrency import run_in_threadpool

from . import accounts, call_context, companion_admin, russian_voice

SAMPLE_RATE = 16000
MAX_PCM = 20 * SAMPLE_RATE * 2


async def frame(socket):
    message = json.loads(await socket.recv())
    if not isinstance(message, dict) or message.get("type") == "error":
        raise ValueError("Invalid voice response")
    return message


async def transcribe(pcm):
    url = os.getenv("MIRA_WHISPER_URL", "ws://127.0.0.1:23334/ws")
    async with connect(url, open_timeout=4, close_timeout=1, max_size=65536) as socket:
        await socket.send(json.dumps({"type": "config", "sample_rate": SAMPLE_RATE, "language": "ru"}))
        for start in range(0, len(pcm), 16000):
            data = base64.b64encode(pcm[start:start + 16000]).decode("ascii")
            await socket.send(json.dumps({"type": "audio", "data": data}))
        await socket.send('{"type":"eou"}')
        while True:
            reply = await frame(socket)
            if reply.get("type") == "final" and isinstance(reply.get("text"), str):
                return reply["text"].strip()
            if reply.get("type") != "ready":
                raise ValueError("Unexpected STT frame")


async def kokoro(text):
    url = os.getenv("MIRA_KOKORO_URL", "ws://127.0.0.1:23335/ws")
    async with connect(url, open_timeout=4, close_timeout=1, max_size=262144) as socket:
        if (await frame(socket)).get("type") != "ready":
            raise ValueError("Kokoro unavailable")
        await socket.send(json.dumps({"type": "config", "voice": "af_heart", "speed": 1.0}))
        if (await frame(socket)).get("type") != "ready":
            raise ValueError("Kokoro configuration failed")
        await socket.send(json.dumps({"type": "text", "text": text}))
        pcm = bytearray()
        while True:
            reply = await frame(socket)
            if reply.get("type") == "done":
                break
            if reply.get("type") != "audio" or not isinstance(reply.get("data"), str):
                raise ValueError("Unexpected TTS frame")
            pcm.extend(base64.b64decode(reply["data"], validate=True))
            if len(pcm) > 24_000 * 2 * 300:
                raise ValueError("Voice response too large")
    if not pcm or len(pcm) % 2:
        raise ValueError("Invalid PCM")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        wav.writeframes(pcm)
    return buf.getvalue()


async def russian_audio(text):
    return await run_in_threadpool(russian_voice.synthesize, text)


async def reply_audio(text):
    if re.search(r"[a-z]", text, re.I) and not re.search(r"[А-Яа-яЁё]", text):
        return await kokoro(text)
    return await russian_audio(text)


def decode_recording(data):
    if not isinstance(data.get("audio"), str):
        raise HTTPException(422, "Запись отсутствует.")
    try:
        pcm = base64.b64decode(data["audio"], validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(422, "Некорректная запись.")
    if not 3200 <= len(pcm) <= MAX_PCM or len(pcm) % 2:
        raise HTTPException(422, "Запишите фразу длительностью до 20 секунд.")
    return pcm


def install(app, repo, dialog, call_reply):
    slots = asyncio.Semaphore(4)

    def authorize(request, operation):
        user = accounts.current_user(request)
        accounts.mutation(request)
        if not accounts.agreements_current(repo, user["id"]):
            raise HTTPException(403, "Подтвердите условия голосового общения в профиле.")
        accounts.limited(repo, "voice:" + operation + ":" + user["id"], 12, 60)
        return user

    async def run(user_id, action, provider, argument):
        if slots.locked():
            raise HTTPException(429, "Голос сейчас занят. Повторите через несколько секунд.")
        async with slots:
            try:
                value = await asyncio.wait_for(provider(argument), timeout=45)
            except (OSError, TimeoutError, ValueError, WebSocketException) as exc:
                with repo.connection() as db:
                    companion_admin.record(db, user_id, action + "_failed")
                raise HTTPException(503, "Голосовой сервис недоступен. Попробуйте ещё раз.") from exc
        with repo.connection() as db:
            companion_admin.record(db, user_id, action)
        return value

    @app.post("/api/chat/transcribe")
    async def recognize(request: Request):
        user_id = authorize(request, "stt")["id"]
        data = await accounts.body(request, (MAX_PCM * 4 // 3) + 64)
        pcm = decode_recording(data)
        text = await run(user_id, "transcribed", transcribe, pcm)
        if not text:
            raise HTTPException(422, "Не удалось разобрать слова. Попробуйте ещё раз.")
        if len(text) > 2000:
            raise HTTPException(422, "Фраза слишком длинная. Разделите её на части.")
        return {"text": text}

    @app.post("/api/chat/call/turn")
    async def call_turn(request: Request):
        user = authorize(request, "call")
        accounts.limited(repo, "chat:" + user["id"], 8, 60)
        data = await accounts.body(request, MAX_PCM * 4 // 3 + 66000)
        pcm = decode_recording(data)
        box = call_context.cipher(app.state.settings.data_dir)
        history = call_context.unpack(box, user["session_hash"], data.get("context"))

        async def exchange(pcm):
            text = (await transcribe(pcm)).strip()
            if not text:
                raise HTTPException(422, "Не расслышала. Скажите ещё раз.")
            if len(text) > 2000:
                raise HTTPException(422, "Фраза слишком длинная. Попробуйте короче.")
            answer = await run_in_threadpool(call_reply, history, text)
            # Preserve full sentences when a model unexpectedly answers at essay length.
            if len(answer) > 800:
                sentences = re.split(r"(?<=[.!?…])\s+", answer[:800])
                answer = " ".join(sentences[:-1]) or answer[:800].rsplit(" ", 1)[0] + "."
            audio = await reply_audio(answer)
            return {"audio": base64.b64encode(audio).decode("ascii"),
                    "context": call_context.pack(box, user["session_hash"], history + [[text, answer]])}

        return await run(user["id"], "call_turn", exchange, pcm)

    @app.post("/api/chat/speak")
    async def speak(request: Request):
        user_id = authorize(request, "tts")["id"]
        data = await accounts.body(request)
        with repo.connection() as db:
            _, turns = dialog(db, user_id)
        turn = next((t for t in turns if t["id"] == data.get("id")), None)
        if not turn:
            raise HTTPException(404, "Сообщение уже удалено.")
        text = turn["assistant"]
        audio = await run(user_id, "synthesized", reply_audio, text)
        return Response(audio, media_type="audio/wav")
