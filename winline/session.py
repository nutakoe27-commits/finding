"""Keep source observations moving during long maintenance requests."""

import json
import time
from pathlib import Path

try:
    from .browser import REFRESH_WATCHES, SNAPSHOT
    from .storage import save_snapshot
except ImportError:
    from browser import REFRESH_WATCHES, SNAPSHOT
    from storage import save_snapshot


class SourceSession:
    def __init__(self, page, db, capture, database, interval=10, light=False):
        self.page, self.db, self.capture = page, db, capture
        self.requests = Path(database).parent / "requests.json"
        self.interval, self.last = interval, 0
        self.data = None
        self.light = light
        self.pending = set()

    def tick(self, force=False, event_ids=()):
        self.pending.update(event_ids)
        if not force and time.monotonic() - self.last < self.interval:
            return None
        try:
            request = json.loads(self.requests.read_text())
            ids = (
                request.get("events", [])
                if time.time() - request.get("at", 0) < 120
                else []
            )
            ids = [
                i
                for i in ids
                if isinstance(i, int) and not isinstance(i, bool) and i > 0
            ]
        except (OSError, ValueError, TypeError):
            ids = []
        if ids:
            self.page.evaluate(REFRESH_WATCHES, ids)
        if self.light:
            self.data = json.loads(
                self.page.evaluate(
                    SNAPSHOT,
                    {"all_lines": False, "event_ids": sorted(self.pending | set(ids))},
                )
            )
        else:
            self.data = json.loads(self.page.evaluate(SNAPSHOT))
        save_snapshot(self.db, self.capture, self.data)
        self.pending.clear()
        self.last = time.monotonic()
        return self.data
