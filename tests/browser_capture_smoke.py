"""Offline acceptance test for the collector, using a synthetic local website.

Does not assert that the live bookmaker adapters work.
"""
import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import sys
from threading import Thread

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from matchlab.capture import collect


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/api/sports"):
            body = json.dumps({"events": [{"id": "test-12", "homeTeam": "Arsenal", "awayTeam": "Chelsea", "odds": [2.1, 3.2, 3.4]}],
                               "token": "private-body-token"}).encode()
            content_type = "application/json"
        elif self.path.startswith("/api/account"):
            body = json.dumps({"email": "private-account-email"}).encode()
            content_type = "application/json"
        else:
            body = b'''<!doctype html><meta charset="utf-8"><h1>Football</h1>
            <div data-at-el="feed-tournament-header" data-at-id="test-league">England Premier League</div>
            <a data-at-el="feed-event" href="/sport/football/12">Arsenal - Chelsea: 1X2 2.10 / 3.20 / 3.40</a>
            <input type="password" value="private-input-password">
            <script>fetch('/api/sports?token=private-query-token', {headers:{Authorization:'Bearer private-header-token'}});fetch('/api/account');</script>'''
            content_type = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Set-Cookie", "private-response-cookie")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


async def run(executable):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        urls = {source: f"http://127.0.0.1:{server.server_port}/" for source in ("betboom", "winline")}
        document = await collect(list(urls), 1, headless=True, executable=executable, entrypoints=urls, quiet=True)
        encoded = json.dumps(document)
        for secret in ("private-body-token", "private-query-token", "private-header-token", "private-input-password", "private-response-cookie", "private-account-email"):
            assert secret not in encoded, secret
        for source in urls:
            assert document["stats"][source]["http_json"] >= 1
            assert document["stats"][source]["public_dom"] >= 1
            assert document["stats"][source]["sports_candidates"] >= 2
            assert any(r["source"] == source and "Arsenal" in json.dumps(r["payload"]) for r in document["records"])
        print("PASS: two-source real browser capture, JSON and visible DOM, timestamped records, query/body/header/form privacy")
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", default=shutil.which("chromium"))
    args = parser.parse_args()
    asyncio.run(run(args.chromium))
