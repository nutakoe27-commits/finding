"""Read-only navigation and card context for Winline's public football line."""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urljoin, urlsplit

from .sports import sport_from_url

LINE_URL = "https://winline.ru/stavki"
FOOTBALL_PATH = "/stavki/sport/futbol"


def statistics_id_from_url(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or parsed.hostname not in ('winline.ru', 'www.winline.ru') or parsed.username or parsed.password:
        return None
    match = re.fullmatch(r'/stats/match/(\d{1,20})/?', parsed.path)
    return match[1] if match else None


def event_statistics_link(links: list[str]) -> tuple[str | None, str]:
    """Use only an observed unambiguous link; stats IDs are a separate space."""
    ids = {value for link in links if isinstance(link, str) and (value := statistics_id_from_url(link))}
    if len(ids) == 1:
        return 'https://winline.ru/stats/match/' + ids.pop(), 'found'
    return None, 'ambiguous' if ids else 'missing'


def event_id_from_url(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname not in ("winline.ru", "www.winline.ru"):
        return None
    match = re.fullmatch(r"(?:/(?:stavki|live)/event|/(?:stavki|live)/sport/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+){0,6})/(\d{1,20})/?", parsed.path)
    if '/sport/' in parsed.path and not sport_from_url(value):
        return None
    return match[1] if match else None


def public_event_id(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or parsed.hostname not in ('winline.ru', 'www.winline.ru'):
        return None
    match = re.fullmatch(r'(?:/(?:stavki|live)/event|/(?:stavki|live)/sport/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+){0,6})/(\d{1,20})/?', parsed.path)
    return match[1] if match else None

CARD_CONTEXT = r"""el => {
    const text = node => {
        let value = node.innerText || '';
        for (const form of node.querySelectorAll('form')) {
            if (form.innerText) value = value.split(form.innerText).join('[form omitted]');
        }
        return value;
    };
    const anchor = el.matches('a[href]') ? el : null;
    if (!anchor || !/\/stavki\/event\/\d+\/?$/.test(new URL(anchor.href).pathname)) return null;
    const tournament = anchor.closest('.block-tournament');
    if (!tournament) return null;
    const eventId = new URL(anchor.href).pathname.replace(/\/$/, '').split('/').pop();
    let card = anchor;
    for (let parent = anchor.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
        const ids = new Set([...parent.querySelectorAll('a[href*="/stavki/event/"]')].map(link =>
            new URL(link.href).pathname.replace(/\/$/, '').split('/').pop()));
        if (ids.size !== 1 || !ids.has(eventId)) break;
        if (text(parent).length > 20000) break;
        card = parent;
        if (parent === tournament) break;
    }
    const times = [...card.querySelectorAll('time[datetime]')].map(time => time.getAttribute('datetime'));
    return {text: text(card).slice(0, 20000),
        tournament: text(tournament).split('\n').map(value => value.trim()).find(Boolean) || '',
        datetimes: [...new Set(times)].slice(0, 4)};
}"""


async def select_public_line(page) -> dict:
    """Initialize the normal public line without selecting a particular sport."""
    parsed = urlsplit(page.url)
    if parsed.hostname not in ("winline.ru", "www.winline.ru"):
        raise ValueError("Ожидалась публичная страница Winline.")
    if parsed.path.rstrip('/') != '/stavki':
        link = page.get_by_role('link', name='Линия', exact=True).filter(visible=True).first
        target = urlsplit(urljoin(page.url, await link.get_attribute('href', timeout=15000) or ''))
        if target.scheme != 'https' or target.hostname not in ('winline.ru', 'www.winline.ru') or target.path.rstrip('/') != '/stavki':
            raise ValueError('Неожиданная ссылка линии')
        await link.click(timeout=15000)
    await page.wait_for_selector('a[href*="/sport/"]', timeout=20000)
    return {'line_selected': True}


async def select_football_line(page) -> dict:
    """Follow only the observed Line/Football links; never touch price buttons."""
    url = urlsplit(page.url)
    if url.hostname not in ("winline.ru", "www.winline.ru"):
        raise ValueError("Ожидалась публичная страница Winline.")
    if url.path.rstrip("/") != "/stavki":
        line = page.get_by_role("link", name="Линия", exact=True).filter(visible=True).first
        target = urlsplit(urljoin(page.url, await line.get_attribute("href", timeout=15000) or ""))
        if target.scheme != "https" or target.hostname not in ("winline.ru", "www.winline.ru") or target.path.rstrip("/") != "/stavki":
            raise ValueError("Неожиданный адрес ссылки «Линия» Winline.")
        await line.click(timeout=15000)
        await page.wait_for_load_state("domcontentloaded")
    # Read the existing anchor instead of guessing a query parameter or route.
    football = page.locator('a[href]').filter(has_text=re.compile(r"^\s*Футбол(?:\s*\d+)?\s*$")).filter(visible=True)
    await football.first.wait_for(state="visible", timeout=15000)
    chosen = None
    for index in range(await football.count()):
        candidate = football.nth(index)
        target = await candidate.get_attribute("href")
        parsed = urlsplit(urljoin(page.url, target or ""))
        if parsed.scheme == "https" and parsed.hostname in ("winline.ru", "www.winline.ru") and parsed.path.rstrip("/") == FOOTBALL_PATH:
            chosen = candidate
            break
    if chosen is None:
        raise ValueError("Не найдена ссылка футбола в предматчевой линии Winline.")
    await chosen.click(timeout=15000)
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_function("() => !!document.querySelector('.block-tournament a[href*=\"/event/\"]')", timeout=15000)
    # The football page may put in-play competitions first and lazy-load the
    # remainder. Bring the last loaded card into view a bounded number of times.
    steps = 0
    for _ in range(5):
        upcoming = await page.evaluate(r"""() => [...document.querySelectorAll('.block-tournament')].some(block =>
            /(?:Сегодня|Завтра|Послезавтра)\s*(?:в\s*)?\d{1,2}:\d{2}|\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)/i.test(block.innerText || '') ||
            !!block.querySelector('time[datetime]'))""")
        if upcoming:
            break
        await page.evaluate("""() => {
            const cards = document.querySelectorAll('.block-tournament a[href*="/event/"]');
            if (cards.length) cards[cards.length - 1].scrollIntoView({block: 'end'});
            const last = cards[cards.length - 1];
            for (let parent = last && last.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
                if (parent.scrollHeight > parent.clientHeight && /auto|scroll/.test(getComputedStyle(parent).overflowY)) {
                    parent.scrollTop += parent.clientHeight;
                    return;
                }
            }
            window.scrollBy(0, Math.max(500, window.innerHeight));
        }""")
        steps += 1
        await asyncio.sleep(1)
    final = urlsplit(page.url)
    if final.hostname not in ("winline.ru", "www.winline.ru") or final.path.rstrip("/") != FOOTBALL_PATH:
        raise ValueError("Не удалось выбрать футбольную страницу Winline.")
    return {"line_selected": True, "football_selected": True, "scroll_steps": steps}


async def open_event_link(page, *, event_id: int | None = None, teams: list[str] | None = None) -> dict:
    """Open a current public event anchor after the homepage has initialized."""
    if teams:
        expression = re.compile(r"^\s*" + re.escape(teams[0]) + r"\s*" + re.escape(teams[1]) + r"\s*$")
        target = page.locator('a[href*="/event/"],a[href*="/sport/"]').filter(has_text=expression).filter(visible=True).first
    elif event_id:
        target = page.locator(f'a[href*="/event/"][href$="/{event_id}"],a[href*="/event/"][href$="/{event_id}/"],a[href*="/sport/"][href$="/{event_id}"],a[href*="/sport/"][href$="/{event_id}/"]').filter(visible=True).first
    else:
        raise ValueError("Нужны ID или обе команды для выбора текущей ссылки.")
    address = urlsplit(urljoin(page.url, await target.get_attribute("href", timeout=15000) or ""))
    selected_id = event_id_from_url(urljoin(page.url, address.geturl()))
    if not selected_id:
        raise ValueError("Неожиданный адрес страницы матча.")
    await target.click(timeout=15000)
    await page.wait_for_url(lambda value: public_event_id(str(value)) == selected_id, timeout=15000)
    return {"event_selected": True, "source_event_id": selected_id}
