"""Minute-based headless collector; only a scoped loopback API key can write data."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import tempfile

import httpx

from .bookmakers import FORMAT, instant, normalize_capture, validate_snapshot
from .capture import collect
from .worker_protocol import SOURCES, parse_sources, source_state, utc_now
from .winline_browser import LINE_URL

ENTRYPOINTS = {"betboom": "https://betboom.ru/sport/football/128/216", "winline": LINE_URL}


def local_api(value: str) -> str:
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL as exc:
        raise ValueError("Некорректный адрес локального Matchlab API.") from exc
    if url.scheme != "http" or url.host not in ("127.0.0.1", "localhost", "::1") or url.userinfo or url.query or url.fragment or url.path not in ("", "/"):
        raise ValueError("Сборщик подключается только к локальному HTTP API. Для другого сервера используйте SSH-туннель.")
    return str(url).rstrip("/")


class Publisher:
    def __init__(self, api_url: str, key: str):
        if not key:
            raise ValueError("Не задан BOOKMAKER_INGEST_TOKEN. Настройте приёмник снимков.")
        self.url = local_api(api_url)
        self.headers = {"X-Matchlab-Collector-Key": key}

    async def request(self, method: str, path: str, body=None):
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                response = await client.request(method, self.url + path, headers=self.headers, json=body)
                response.raise_for_status()
                result = response.json()
            if not isinstance(result, dict):
                raise ValueError("Неожиданный ответ локального API.")
            return result
        except httpx.HTTPStatusError as exc:
            raise ValueError(f"Локальный API вернул HTTP {exc.response.status_code}. Проверьте версию приложения и ключ приёмника.") from exc
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise ValueError("Локальный API недоступен или вернул неверный формат. Ключ в журнал не выводится.") from exc


class MinuteCollector:
    def __init__(self, publisher: Publisher, interval: int, state_dir: Path, sources: tuple[str, ...] = SOURCES):
        self.publisher = publisher
        self.interval = interval
        self.state_dir = state_dir
        self.sources = parse_sources(",".join(sources))
        self.references: dict[str, dict] = {}
        self.last_status = {"state": "starting", "interval_seconds": interval, "sources": {}}
        self.complete_batches = 0

    async def announce(self, status: dict):
        self.last_status = status
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=self.state_dir, encoding="utf-8", delete=False) as output:
            json.dump({**status, "attempted_at": utc_now()}, output)
            temporary = Path(output.name)
        try:
            temporary.replace(self.state_dir / "status.json")
        finally:
            temporary.unlink(missing_ok=True)
        await self.publisher.request("POST", "/api/bookmakers/worker-status", status)

    async def bootstrap(self):
        await self.announce(self.last_status)
        try:
            existing = await self.publisher.request("GET", "/api/bookmakers")
            for item in existing.get("quotes", []):
                row = validate_snapshot(item)
                if row["source"] == "betboom":
                    self.references[row["event_key"]] = row
        except (ValueError, TypeError, KeyError):
            # Missing old reference data is not permission to guess a sport.
            self.references = {}

    async def process(self, document: dict):
        states = {source: source_state(document, source) for source in self.sources}
        accepted = dict(document)
        # A frozen DOM after WebSocket loss is never re-stamped as fresh odds.
        accepted["records"] = [record for record in document.get("records", []) if states.get(record.get("source")) == "ok"]
        now = datetime.now(timezone.utc)
        self.references = {key: row for key, row in self.references.items() if instant(row["kickoff_at"]) > now}
        normalized = normalize_capture(accepted, list(self.references.values()))
        latest = {}
        for row in normalized["snapshots"]:
            if row["observed_at"] >= latest.get(row["event_key"], {}).get("observed_at", ""):
                latest[row["event_key"]] = row
        # A minute sample contains one quote per event, not every DOM reread.
        normalized["snapshots"] = list(latest.values())
        for source in SOURCES:
            normalized["report"]["sources"][source]["snapshots"] = sum(row["source"] == source for row in latest.values())
        for row in normalized["snapshots"]:
            if row["source"] == "betboom":
                self.references[row["event_key"]] = row
        counts = normalized["report"]["sources"]
        source_status = {source: {"state": states[source] if states[source] != "ok" else "ok" if counts[source]["events"] else "empty",
                                   "events": counts[source]["events"]} for source in self.sources}
        good = sum(item["state"] == "ok" for item in source_status.values())
        state = "ok" if good == len(self.sources) else "partial" if good else "blocked" if any(item["state"] == "blocked" for item in source_status.values()) else "error"
        status = {"state": state, "interval_seconds": self.interval, "sources": source_status}
        if normalized["snapshots"]:
            try:
                reply = await self.publisher.request("POST", "/api/bookmakers/ingest", normalized)
                print(json.dumps({"at": utc_now(), "inserted": reply.get("inserted"), "sources": source_status}, ensure_ascii=False), flush=True)
            except ValueError:
                status["state"] = "error"
                await self.announce(status)
                raise
        else:
            print(json.dumps({"at": utc_now(), "inserted": 0, "sources": source_status}, ensure_ascii=False), flush=True)
        await self.announce(status)
        self.complete_batches += int(state == "ok")


async def run(args):
    publisher = Publisher(args.api_url, os.getenv("BOOKMAKER_INGEST_TOKEN", ""))
    sources = getattr(args, "sources", SOURCES)
    worker = MinuteCollector(publisher, args.interval, args.state_dir, sources)
    await worker.bootstrap()
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    # The local write credential must not be inherited by the site browser.
    browser_env = {key: value for key, value in os.environ.items() if key not in ("BOOKMAKER_INGEST_TOKEN", "QWEN_API_KEY")}

    async def batch(document):
        try:
            await worker.process(document)
        except ValueError:
            print("Не удалось опубликовать пакет. Старые данные сохранены; повторная попытка будет в следующем интервале.", flush=True)

    try:
        await collect(list(worker.sources), args.sample_seconds if args.once else None, headless=True,
                      executable=args.executable, quiet=True, on_batch=batch,
                      batch_seconds=args.interval, stop_event=stopped,
                      block_heavy_resources=True, browser_env=browser_env,
                      entrypoints=ENTRYPOINTS, reconnect_seconds=180, winline_prematch=True)
    except ValueError:
        await worker.announce({"state": "error", "interval_seconds": args.interval, "sources": {}})
        raise
    if args.once:
        print(json.dumps(worker.last_status, ensure_ascii=False), flush=True)
        return 0 if worker.complete_batches and worker.last_status["state"] == "ok" and not stopped.is_set() else 2
    await worker.announce({**worker.last_status, "state": "stopped"})
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=os.getenv("BOOKMAKER_API_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--state-dir", type=Path, default=Path(".local/server-scraper"))
    parser.add_argument("--once", action="store_true", help="Verify every selected source before enabling the daemon")
    parser.add_argument("--sources", type=parse_sources, default=os.getenv("BOOKMAKER_SOURCES", "winline"), metavar="winline|betboom,winline")
    parser.add_argument("--sample-seconds", type=int, default=30)
    parser.add_argument("--executable")
    args = parser.parse_args()
    if not 10 <= args.interval <= 3600 or not 1 <= args.sample_seconds <= 180:
        parser.error("Некорректный интервал или длительность проверки.")
    try:
        raise SystemExit(asyncio.run(run(args)))
    except (ValueError, OSError, ImportError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "Не удалось запустить сборщик. Проверьте установку браузера и доступ к каталогу состояния."
        parser.exit(1, message + "\n")


if __name__ == "__main__":
    main()
