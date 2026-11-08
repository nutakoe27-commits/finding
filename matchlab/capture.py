"""Read-only discovery of public bookmaker data in a fresh browser context.

This produces evidence for source adapters, not normalized matches or verified
odds. In particular a protobuf WebSocket is not treated as parsed JSON.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

FORMAT = "matchlab.bookmaker-capture.v1"
SOURCES = {
    "betboom": "https://betboom.ru/",
    "winline": "https://winline.ru/",
}
PRIVATE_PATH = re.compile(
    r"(?:^|/)(?:auth|login|logout|register|registration|profile|account|cabinet|cashier|payment|payments|wallet|withdrawal|bets[_-]?history[^/]*)(?:/|$)", re.I
)
SENSITIVE_KEY = re.compile(
    r"authorization|cookie|token|password|passwd|secret|session|csrf|xsrf|api.?key|access.?key|"
    r"email|phone|user.?id|user.?name|account|balance|payment|wallet|passport|fingerprint|device.?id", re.I
)
SPORT_KEYS = {
    "events", "matches", "markets", "odds", "outcomes", "factors", "coefficients",
    "tournaments", "competitors", "participants", "hometeam", "awayteam", "team1", "team2",
}
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 12 * 1024 * 1024
MAX_PAYLOADS_PER_SOURCE = 100


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_url(value: str) -> str:
    """Keep endpoint paths, but never query values, userinfo or fragments."""
    try:
        url = urlsplit(value)
        if url.scheme not in ("http", "https", "ws", "wss") or not url.hostname:
            return "[non-network-url]"
        host = url.hostname
        if ":" in host:
            host = f"[{host}]"
        if url.port:
            host += f":{url.port}"
        path = "/[private-path]" if PRIVATE_PATH.search(url.path) else url.path
        query = urlencode([(key, "[redacted]") for key, _ in parse_qsl(url.query)])
        return urlunsplit((url.scheme, host, path, query, ""))
    except ValueError:
        return "[invalid-url]"


def redact(value: Any, depth: int = 0) -> Any:
    if depth > 40:
        return "[depth-limit]"
    if isinstance(value, dict):
        return {str(key): "[redacted]" if SENSITIVE_KEY.search(str(key)) else redact(item, depth + 1)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, depth + 1) for item in value]
    if isinstance(value, str):
        if value.startswith(("https://", "http://", "wss://", "ws://")):
            return safe_url(value)
        if value.startswith("Bearer ") or re.fullmatch(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", value):
            return "[redacted]"
    return value


def sports_json(value: Any) -> bool:
    keys = set()
    stack = [(value, 0)]
    visited = 0
    while stack and visited < 50000:
        current, depth = stack.pop()
        visited += 1
        if depth > 30:
            continue
        if isinstance(current, dict):
            keys.update(re.sub(r"[^a-z]", "", str(key).lower()) for key in current)
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        if len(keys & SPORT_KEYS) >= 2:
            return True
    return False


def sports_text(value: str) -> bool:
    football = re.search(r"футбол|football|soccer", value, re.I)
    outcomes = re.search(r"(?:П1|П2|1X2|1Х2|коэффициент|\bodds\b)", value, re.I)
    return bool(football and outcomes)


class Capture:
    def __init__(self, sources: list[str]):
        self.started_at = now()
        self.sources = sources
        self.records: list[dict] = []
        self.stats = {source: Counter() for source in sources}
        self.bytes = 0
        self.counts = Counter()
        self.network_counts = Counter()
        self.duplicates: dict[tuple[str, str, str, str], dict] = {}
        self.dom_hashes: dict[tuple[str, str], str] = {}
        self.endpoints: dict[tuple[str, str, str], dict] = {}
        self.navigation: dict[str, dict] = {}

    def endpoint(self, source: str, kind: str, url: str, status: int | None = None, content_type: str = ""):
        clean = safe_url(url)
        if "[private-path]" in clean:
            return
        key = (source, kind, clean)
        if key not in self.endpoints and sum(k[0] == source for k in self.endpoints) >= 250:
            self.stats[source]["endpoint_limit"] += 1
            return
        record = self.endpoints.setdefault(key, {"source": source, "kind": kind, "url": clean, "count": 0})
        record["count"] += 1
        if status is not None:
            record["status"] = status
        if content_type:
            record["content_type"] = content_type.split(";")[0]

    def add(self, source: str, kind: str, url: str, payload: Any, candidate: bool, **extra) -> bool:
        if "[private-path]" in safe_url(url):
            return False
        cleaned = redact(payload)
        encoded = json.dumps(cleaned, ensure_ascii=False).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        key = (source, kind, safe_url(url), digest)
        if key in self.duplicates:
            previous = self.duplicates[key]
            previous["last_observed_at"] = now()
            previous["repeat_count"] += 1
            self.stats[source]["duplicate_payloads"] += 1
            return True
        record_limit = self.stats[source]["public_dom"] >= 24 if kind == "public_dom" else self.network_counts[source] >= MAX_PAYLOADS_PER_SOURCE
        if len(encoded) > MAX_BODY_BYTES or self.bytes + len(encoded) > MAX_TOTAL_BYTES or record_limit:
            self.stats[source]["payload_limit"] += 1
            return False
        record = {"source": source, "kind": kind, "url": safe_url(url),
                             "observed_at": now(), "sports_candidate": candidate,
                             "sha256": digest, "payload": cleaned, "repeat_count": 1, **extra}
        self.records.append(record)
        self.duplicates[key] = record
        self.counts[source] += 1
        self.network_counts[source] += int(kind != "public_dom")
        self.bytes += len(encoded)
        self.stats[source][kind] += 1
        self.stats[source]["sports_candidates"] += int(candidate)
        return True

    def frame(self, source: str, url: str, data: str | bytes):
        if isinstance(data, bytes):
            self.stats[source]["binary_websocket_frames"] += 1
            return  # Binary feeds need their actual schema; never invent a decode.
        if len(data.encode()) > MAX_BODY_BYTES:
            self.stats[source]["oversized_frames"] += 1
            return
        try:
            payload = json.loads(data)
        except (ValueError, TypeError):
            self.stats[source]["non_json_websocket_frames"] += 1
            return
        if sports_json(payload):
            self.add(source, "websocket_json", url, payload, True)

    def dom(self, source: str, url: str, text: str, nodes: list[dict], truncated: bool = False):
        if PRIVATE_PATH.search(urlsplit(url).path) or not sports_text(text):
            return
        digest = hashlib.sha256(json.dumps([text, nodes], ensure_ascii=False).encode()).hexdigest()
        key = (source, safe_url(url))
        if self.dom_hashes.get(key) == digest:
            return
        if self.add(source, "public_dom", url, {"text": text, "nodes": nodes}, True, truncated=truncated):
            self.dom_hashes[key] = digest

    def document(self) -> dict:
        return {"format": FORMAT, "started_at": self.started_at, "finished_at": now(),
                "scope": "anonymous public pages; no normalized matches or verified odds",
                "sources": self.sources, "navigation": self.navigation,
                "stats": {key: dict(value) for key, value in self.stats.items()},
                "endpoints": list(self.endpoints.values()), "records": self.records}


DOM_SCRIPT = r"""() => {
    const text = document.body ? document.body.innerText : '';
    const selector = '[data-at-el], [data-testid], [data-test], a[href*="/sport/"], a[href*="/event/"], a[href*="/match/"]';
    const elements = [...document.querySelectorAll(selector)].filter(el =>
        el.getClientRects().length && !el.closest('form') && !['INPUT','TEXTAREA'].includes(el.tagName));
    const nodes = elements.slice(0, 500).map(el => {
        const attrs = {};
        for (const name of ['data-at-el','data-at-id','data-at-name','data-testid','data-test','class']) {
            if (el.hasAttribute(name)) attrs[name] = el.getAttribute(name).slice(0, 500);
        }
        if (el.tagName === 'A') attrs.href = el.href;
        return {tag: el.tagName.toLowerCase(), attrs, text: (el.innerText || '').slice(0, 1200)};
    });
    return {text: text.slice(0, 200000), nodes, truncated: text.length > 200000 || elements.length > 500};
}"""


async def collect(sources: list[str], seconds: float, *, headless: bool = False,
                  executable: str | None = None, entrypoints: dict[str, str] | None = None,
                  quiet: bool = False) -> dict:
    from playwright.async_api import Error as PlaywrightError, async_playwright

    capture = Capture(sources)
    entrypoints = entrypoints or SOURCES
    pending: set[asyncio.Task] = set()
    closing = False
    pages = []

    async def response_received(source, response):
        try:
            url = response.url
            if PRIVATE_PATH.search(urlsplit(url).path):
                return
            content_type = response.headers.get("content-type", "")
            if response.request.resource_type not in ("fetch", "xhr") and "json" not in content_type:
                return
            capture.endpoint(source, "http", url, response.status, content_type)
            if response.status != 200 or "json" not in content_type.lower():
                return
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
                capture.stats[source]["oversized_responses"] += 1
                return
            body = await response.body()
            if len(body) > MAX_BODY_BYTES:
                capture.stats[source]["oversized_responses"] += 1
                return
            payload = json.loads(body)
            host = (urlsplit(url).hostname or "").lower()
            base_host = urlsplit(entrypoints[source]).hostname or ""
            own_host = host == base_host or host.endswith("." + base_host)
            candidate = sports_json(payload)
            if own_host or candidate:
                capture.add(source, "http_json", url, payload, candidate)
        except (ValueError, TypeError):
            capture.stats[source]["invalid_json"] += 1
        except Exception:
            # Browser/network errors can contain cookies or request URLs.
            # Record only the class of failure, not upstream exception text.
            capture.stats[source]["response_read_errors"] += 1

    def enqueue(source, response):
        if closing or len(pending) >= 12:
            capture.stats[source]["pending_limit"] += 1
            return
        task = asyncio.create_task(response_received(source, response))
        pending.add(task)
        task.add_done_callback(pending.discard)

    def websocket_opened(source, websocket):
        capture.endpoint(source, "websocket", websocket.url)
        websocket.on("framereceived", lambda data: capture.frame(source, websocket.url, data))

    async with async_playwright() as playwright:
        launch = {"headless": headless}
        if executable:
            launch["executable_path"] = executable
        try:
            browser = await playwright.chromium.launch(**launch)
        except PlaywrightError as exc:
            raise ValueError("Не удалось запустить Chromium. Выполните python -m playwright install chromium; для сервера без рабочего стола используйте --headless.") from exc
        try:
            # New, nonpersistent profile: no access to the user's normal cookies.
            context = await browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow")

            async def open_page(source):
                page = await context.new_page()
                pages.append((source, page))
                page.on("response", lambda response: enqueue(source, response))
                page.on("websocket", lambda websocket: websocket_opened(source, websocket))
                try:
                    response = await page.goto(entrypoints[source], wait_until="domcontentloaded", timeout=45000)
                    capture.navigation[source] = {"url": safe_url(page.url), "status": response.status if response else None}
                except Exception:
                    capture.navigation[source] = {"url": safe_url(entrypoints[source]), "error": "navigation_failed"}

            await asyncio.gather(*(open_page(source) for source in sources))
            if not quiet:
                print("Открыты отдельные вкладки BetBoom/Winline. Выберите футбол, предматчевую линию и откройте карточку матча.", flush=True)
                print("Используйте только публичные страницы, без входа в аккаунт. Сборщик сам ничего не нажимает и не делает ставки.", flush=True)
            deadline = asyncio.get_running_loop().time() + seconds
            last_progress = 0.0
            while asyncio.get_running_loop().time() < deadline:
                for source, page in pages:
                    if page.is_closed():
                        continue
                    for frame in page.frames:
                        if PRIVATE_PATH.search(urlsplit(frame.url).path):
                            continue
                        try:
                            snapshot = await asyncio.wait_for(frame.evaluate(DOM_SCRIPT), timeout=4)
                            capture.dom(source, frame.url, **snapshot)
                        except Exception:
                            capture.stats[source]["dom_read_errors"] += 1
                current = asyncio.get_running_loop().time()
                if not quiet and current - last_progress >= 15:
                    print("Сохранено снимков:", dict(capture.counts), flush=True)
                    last_progress = current
                await asyncio.sleep(max(0, min(3, deadline - asyncio.get_running_loop().time())))
        finally:
            closing = True
            if pending:
                await asyncio.wait(pending, timeout=5)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            await browser.close()
    return capture.document()


def save_document(document: dict, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: never overwrite an existing capture or follow a link.
    with destination.open("x", encoding="utf-8") as output:
        json.dump(document, output, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def summary(document: dict) -> dict:
    if document.get("format") != FORMAT:
        raise ValueError("Неизвестный формат файла захвата.")
    return {"format": FORMAT, "started_at": document["started_at"], "finished_at": document["finished_at"],
            "navigation": document["navigation"], "stats": document["stats"],
            "records": len(document["records"]), "endpoints": len(document["endpoints"]),
            "normalized_matches": False,
            "needs_source_adapter_validation": True}


def main():
    parser = argparse.ArgumentParser(description="Capture public bookmaker pages for adapter development; does not place bets")
    parser.add_argument("--source", choices=["both", *SOURCES], default="both")
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--inspect", type=Path, help="Print only capture metadata; do not launch a browser")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--executable", help="Optional path to an installed Chromium browser")
    args = parser.parse_args()
    try:
        if args.inspect:
            print(json.dumps(summary(json.loads(args.inspect.read_text(encoding="utf-8"))), ensure_ascii=False, indent=2))
            return
        if not 1 <= args.seconds <= 3600:
            parser.error("--seconds должен быть от 1 до 3600")
        destination = args.output or Path(".local/bookmaker-captures") / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
        if destination.exists() or destination.is_symlink():
            parser.error("Файл назначения уже существует; выберите новое имя.")
        sources = list(SOURCES) if args.source == "both" else [args.source]
        document = asyncio.run(collect(sources, args.seconds, headless=args.headless, executable=args.executable))
        save_document(document, destination)
        print("Файл:", destination.resolve())
        print(json.dumps(summary(document), ensure_ascii=False, indent=2))
        if any(not document["stats"][source].get("sports_candidates", 0) for source in sources):
            print("Часть источников не дала распознаваемых публичных спортивных данных. Файл диагностики сохранён; готовность скрапера не подтверждена.")
            raise SystemExit(2)
    except ImportError:
        parser.exit(1, "Нужен Playwright: установите requirements-capture.txt и выполните python -m playwright install chromium.\n")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Ошибка: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Сбор прерван. Запустите ещё раз и дождитесь сохранения файла.\n")


if __name__ == "__main__":
    main()
