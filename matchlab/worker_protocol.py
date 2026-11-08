"""Small shared contract for the local server scraper, without model imports."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

SOURCES = ("betboom", "winline")
STATES = {"starting", "ok", "partial", "blocked", "disconnected", "unavailable", "empty", "error", "stopped"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_status(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("state"), str) or value["state"] not in STATES:
        raise ValueError("Некорректное состояние сборщика.")
    interval = value.get("interval_seconds", 60)
    if isinstance(interval, bool) or not isinstance(interval, int) or not 10 <= interval <= 3600:
        raise ValueError("Некорректный интервал сборщика.")
    incoming = value.get("sources", {})
    if not isinstance(incoming, dict) or set(incoming) - set(SOURCES):
        raise ValueError("Некорректные источники сборщика.")
    sources = {}
    for source, item in incoming.items():
        if not isinstance(item, dict) or not isinstance(item.get("state"), str) or item["state"] not in STATES:
            raise ValueError("Некорректное состояние источника.")
        count = item.get("events", 0)
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 10000:
            raise ValueError("Некорректный счётчик событий.")
        sources[source] = {"state": item["state"], "events": count}
    return {"state": value["state"], "interval_seconds": interval, "sources": sources}


def market_feed(source: str, value: str) -> bool:
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if source == "betboom":
        return host.endswith(".sporthub.bet") and parsed.path.rstrip("/") == "/api/tree_ws/v1"
    return source == "winline" and host.endswith(".winline.ru") and parsed.path.rstrip("/") == "/data_ng"


def source_state(document: dict, source: str) -> str:
    connections = [item for item in document.get("transport", {}).get(source, []) if market_feed(source, item.get("url", ""))]
    for item in connections:
        try:
            last_activity = datetime.fromisoformat(item.get("last_frame_at"))
            age = (datetime.now(timezone.utc) - last_activity).total_seconds()
            if item.get("connected") is True and 0 <= age <= 180:
                return "ok"
        except (KeyError, TypeError, ValueError):
            continue
    navigation = document.get("navigation", {}).get(source, {})
    if navigation.get("status") in (401, 403, 429) or navigation.get("blocked_hint"):
        return "blocked"
    if connections:
        return "disconnected"
    if navigation.get("error") or (isinstance(navigation.get("status"), int) and navigation["status"] >= 500):
        return "unavailable"
    return "disconnected"
