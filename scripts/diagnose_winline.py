#!/usr/bin/env python3
"""Capture public football DOM for adapter repair; never imports or uploads odds."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
if ROOT.name == "scripts":
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

from matchlab.bookmakers import normalize_capture
from matchlab.capture import FORMAT, redact, safe_url, sports_text

URL = "https://winline.ru/stavki/sport/futbol/"
DOM = r"""() => {
    const visible = el => el.getClientRects().length && !el.closest('form') &&
        !['INPUT', 'TEXTAREA', 'SELECT', 'SCRIPT', 'STYLE'].includes(el.tagName);
    const text = (el, limit) => {
        let value = el ? el.innerText || '' : '';
        for (const form of el ? el.querySelectorAll('form') : []) {
            if (form.innerText) value = value.split(form.innerText).join('[form omitted]');
        }
        return value.slice(0, limit);
    };
    const anchors = [...document.querySelectorAll('a[href]')].filter(visible);
    const events = anchors.filter(el => /\/(event|match)\//.test(el.href));
    const others = [...document.querySelectorAll('[data-at-el], [data-testid], [data-test], [role="row"]')].filter(visible);
    // Event anchors precede navigation/decoration, independent of DOM order.
    const elements = [...new Set([...events, ...anchors, ...others])];
    const nodes = elements.slice(0, 500).map(el => {
        const attrs = {};
        for (const name of ['class', 'data-at-el', 'data-at-id', 'data-at-name', 'data-testid', 'data-test', 'role']) {
            if (el.hasAttribute(name)) attrs[name] = el.getAttribute(name).slice(0, 500);
        }
        if (el.tagName === 'A') attrs.href = el.href;
        const node = {tag: el.tagName.toLowerCase(), attrs, text: text(el, 1200)};
        if (events.includes(el)) {
            node.parents = [];
            let parent = el.parentElement;
            for (let depth = 0; parent && parent !== document.body && depth < 5; depth++, parent = parent.parentElement) {
                node.parents.push({tag: parent.tagName.toLowerCase(), class: (parent.className || '').toString().slice(0, 300), text: text(parent, 8000)});
                if ((parent.innerText || '').length > 8000) break;
            }
        }
        return node;
    });
    const bodyText = text(document.body, 250000);
    return {text: bodyText, nodes, title: document.title.slice(0, 300),
        headings: [...document.querySelectorAll('h1,h2,h3,[role="heading"]')].filter(visible).slice(0, 30).map(el => text(el, 200)),
        event_links: events.length, total_nodes: elements.length,
        truncated: (document.body ? document.body.innerText.length : 0) > 250000 || elements.length > 500};
}"""


def report(document: dict) -> dict:
    record = document["records"][0]
    payload = record["payload"]
    events = [node for node in payload["nodes"] if "/event/" in node.get("attrs", {}).get("href", "") or "/match/" in node.get("attrs", {}).get("href", "")]
    examples = []
    for node in events[:2]:
        parents, seen = [], {" ".join(node["text"].split())}
        for parent in node.get("parents", []):
            key = " ".join(parent["text"].split())
            if key not in seen:
                parents.append(parent)
                seen.add(key)
        contexts = parents[:1] + parents[-1:] if len(parents) > 1 else parents
        examples.append({"text": node["text"], "url": node["attrs"]["href"],
                         "parents": [{**parent, "text": parent["text"][:2000]} for parent in contexts]})
    try:
        parsed = normalize_capture(document)["report"]["sources"]["winline"]
    except (ValueError, TypeError, KeyError):
        parsed = {"error": "unsupported_layout"}
    return {"url": record["url"], "navigation": document["navigation"],
            "title": payload["title"], "sports_text_filter": sports_text(payload["text"]),
            "headings": payload["headings"], "event_links": payload["event_links"],
            "total_nodes": payload["total_nodes"], "parsed": parsed,
            "examples": examples,
            "text_sample": payload["text"][:5000] if not examples else payload["text"][:1500]}


async def capture(seconds: int, executable: str | None = None) -> dict:
    from playwright.async_api import Error as PlaywrightError, async_playwright
    async with async_playwright() as playwright:
        launch = {"headless": True, "args": ["--disable-gpu"],
                  "env": {key: value for key, value in os.environ.items() if key not in ("BOOKMAKER_INGEST_TOKEN", "QWEN_API_KEY")}}
        if executable:
            launch["executable_path"] = executable
        browser = await playwright.chromium.launch(**launch)
        try:
            context = await browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow")

            async def lightweight(route):
                if route.request.resource_type in ("image", "media", "font"):
                    await route.abort()
                else:
                    await route.continue_()

            await context.route("**/*", lightweight)
            page = await context.new_page()
            navigation = {}
            try:
                response = await page.goto(URL, wait_until="domcontentloaded", timeout=45000)
                navigation["status"] = response.status if response else None
            except PlaywrightError:
                navigation["error"] = "navigation_failed"
            await asyncio.sleep(seconds)
            # Deliberately independent of the production sports-text filter.
            payload = redact(await page.evaluate(DOM))
            document = {"format": FORMAT, "sources": ["winline"], "navigation": {"winline": navigation},
                        "records": [{"source": "winline", "kind": "public_dom", "url": safe_url(page.url),
                                     "observed_at": datetime.now(timezone.utc).isoformat(), "payload": payload}]}
            return document
        finally:
            await browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--executable")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 60:
        parser.error("--seconds должен быть от 1 до 60")
    if args.output.exists() or args.output.is_symlink():
        parser.error("Файл уже существует; выберите другое имя.")
    os.umask(0o077)
    try:
        document = asyncio.run(capture(args.seconds, args.executable))
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(document, output, ensure_ascii=False, allow_nan=False)
        print(json.dumps(report(document), ensure_ascii=False, indent=2))
        print("Снимок сохранён:", args.output)
    except (OSError, ValueError, ImportError):
        parser.exit(1, "Диагностика не завершена. Проверьте браузер и путь вывода.\n")


if __name__ == "__main__":
    main()
