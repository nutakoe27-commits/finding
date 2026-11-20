"""Measure perceived latency of concurrent synthetic chats through the configured Qwen endpoint."""
import argparse
import asyncio
import json
from statistics import median
from time import perf_counter
from uuid import uuid4

import httpx

from . import qwen
from .config import Settings


async def request(client, url, model, words, output_tokens=64):
    # Distinct prefixes keep one synthetic chat from priming another's prompt.
    text = str(uuid4()) + "\n" + "город " * words + "\nПридумай и опиши прогулку по этому городу в десяти предложениях."
    body = {"model": model, "messages": [{"role": "user", "content": text}], "stream": True,
            "max_tokens": output_tokens, "temperature": 0, "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": False}}
    start = perf_counter()
    first = None
    usage = {}
    async with client.stream("POST", url, json=body) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            event = json.loads(data)
            if event.get("usage"):
                usage = event["usage"]
            for choice in event.get("choices", []):
                if first is None and (choice.get("delta") or {}).get("content"):
                    first = perf_counter()
    if first is None:
        raise ValueError("Пустой ответ")
    return first - start, perf_counter() - start, usage.get("prompt_tokens"), usage.get("completion_tokens")


async def run(settings, levels, rounds, words, target, timeout):
    url = qwen._base_url(settings) + "/chat/completions"
    async with httpx.AsyncClient(headers=qwen._headers(settings), timeout=timeout or settings.qwen_timeout) as client:
        if target:
            short = (await request(client, url, settings.qwen_model, 64, 2))[2]
            sample = (await request(client, url, settings.qwen_model, 1024, 2))[2]
            if short is None or sample is None or sample <= short:
                raise ValueError("Strata не вернула счётчик входных токенов для калибровки")
            words = max(1, round(64 + (target - short) * 960 / (sample - short)))
        else:
            await request(client, url, settings.qwen_model, min(words, 100))
        for n in levels:
            rows = []
            started = perf_counter()
            for _ in range(rounds):
                rows.extend(await asyncio.gather(*(request(client, url, settings.qwen_model, words) for _ in range(n))))
            elapsed = perf_counter() - started
            tokens = [row[3] for row in rows]
            inputs = [row[2] for row in rows if row[2] is not None]
            print(json.dumps({"parallel": n, "requests": len(rows), "first_text_median_s": round(median(r[0] for r in rows), 3),
                              "first_text_worst_s": round(max(r[0] for r in rows), 3),
                              "reply_median_s": round(median(r[1] for r in rows), 3),
                              "prompt_tokens_range": [min(inputs), max(inputs)] if inputs else None,
                              "total_output_tok_s": round(sum(tokens) / elapsed, 2) if all(tokens) else None}), flush=True)


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--parallel", nargs="+", type=int, choices=(1, 2, 4, 6), default=[1, 2, 4, 6])
    cli.add_argument("--rounds", type=int, default=3)
    cli.add_argument("--words", type=int, default=500)
    cli.add_argument("--input-tokens", type=int, help="approximate input length, calibrated with short real requests")
    cli.add_argument("--timeout", type=float, help="HTTP read timeout per request, in seconds")
    args = cli.parse_args()
    if args.rounds < 1 or args.words < 1:
        cli.error("--rounds и --words должны быть положительными")
    if args.input_tokens is not None and args.input_tokens < 1 or args.timeout is not None and args.timeout <= 0:
        cli.error("Длина и таймаут должны быть положительными")
    asyncio.run(run(Settings.from_env(), args.parallel, args.rounds, args.words, args.input_tokens, args.timeout))
