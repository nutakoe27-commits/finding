"""Football pre-match 1X2 adapters, validated against captured public DOM.

All numeric outcomes are from one bookmaker, market and observation. Binary
WebSockets and unrelated JSON are never presented as decoded market data.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .capture import FORMAT as CAPTURE_FORMAT, safe_url

FORMAT = "matchlab.bookmaker-snapshots.v1"
VERSION = "public-dom-1x2-v3"
EPL = "Англия. Премьер-лига"
KEYS = ("home", "draw", "away")
ALIASES = {
    "Арсенал": "Arsenal", "Астон Вилла": "Aston Villa", "Борнмут": "Bournemouth",
    "Брентфорд": "Brentford", "Брайтон": "Brighton", "Бёрнли": "Burnley", "Бернли": "Burnley",
    "Челси": "Chelsea", "Кристал Пэлас": "Crystal Palace", "Эвертон": "Everton", "Фулхэм": "Fulham",
    "Ипсвич Таун": "Ipswich", "Ипсвич": "Ipswich", "Лидс": "Leeds", "Лестер": "Leicester",
    "Ливерпуль": "Liverpool", "Лутон": "Luton", "Манчестер Сити": "Man City",
    "Манчестер Юн": "Man United", "Манчестер Юнайтед": "Man United", "Ньюкасл": "Newcastle",
    "Ноттингем Форест": "Nott'm Forest", "Норвич": "Norwich", "Шеффилд Юнайтед": "Sheffield United",
    "Саутгемптон": "Southampton", "Сандерленд": "Sunderland", "Тоттенхэм": "Tottenham",
    "Уотфорд": "Watford", "Вест Бромвич": "West Brom", "Вест Хэм": "West Ham", "Вулверхэмптон": "Wolves",
}
ALIASES.update({value: value for value in list(ALIASES.values())})
MONTHS = {word: i + 1 for i, word in enumerate(("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"))}
DATE_RE = re.compile(r"^(?:(Сегодня|Завтра|Послезавтра)|(?:(\d{1,2})\s+([а-яё]+)(?:\s+(20\d{2}))?))(?:\s+в)?\s+(\d{1,2}):(\d{2})$", re.I)
ODDS_RE = re.compile(r"(?m)^П1\n(\d+(?:[.,]\d+)?)\n[XХ]\n(\d+(?:[.,]\d+)?)\nП2\n(\d+(?:[.,]\d+)?)$")
FORM_RE = re.compile(r"^\d+[-–]\d+[-–]\d+$")


def instant(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ValueError("Нужна дата и время в формате ISO 8601 с часовым поясом.") from exc
    if result.tzinfo is None:
        raise ValueError("Время снимка и начала должно содержать часовой пояс.")
    return result.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def team_key(value: str) -> str:
    text = " ".join(value.split())
    text = {"Зенит СПб": "Зенит", "Акрон Тольятти": "Акрон"}.get(text, text)
    return ALIASES.get(text, text).casefold()


def kickoff(label: str, observed_at: str) -> str:
    match = DATE_RE.fullmatch(label.strip())
    if not match:
        raise ValueError("Неизвестный формат времени начала.")
    local = instant(observed_at).astimezone(ZoneInfo("Europe/Moscow"))
    relative, day, month, year, hour, minute = match.groups()
    if relative:
        target = local + timedelta(days={"сегодня": 0, "завтра": 1, "послезавтра": 2}[relative.lower()])
        target = target.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
    else:
        month_number = MONTHS.get(month.lower())
        if month_number is None:
            raise ValueError("Неизвестный месяц.")
        selected_year = int(year) if year else local.year
        if not year and local.month >= 11 and month_number <= 2:
            selected_year += 1
        target = datetime(selected_year, month_number, int(day), int(hour), int(minute), tzinfo=local.tzinfo)
    if target <= local or target > local + timedelta(days=400):
        raise ValueError("Матч уже начался или дата вне предматчевого горизонта.")
    return iso(target)


def lines(value: str) -> list[str]:
    result = [" ".join(line.split()) for line in value.splitlines() if line.strip()]
    # Highlights and detail pages place date and clock on separate lines.
    merged, i = [], 0
    while i < len(result):
        if re.fullmatch(r"Сегодня|Завтра|Послезавтра", result[i], re.I) and i + 1 < len(result) and re.fullmatch(r"\d{1,2}:\d{2}", result[i + 1]):
            merged.append(result[i] + " " + result[i + 1])
            i += 2
        else:
            merged.append(result[i])
            i += 1
    return merged


def make_snapshot(source: str, competition: str, home: str, away: str, start: str,
                  observed: str, odds: list[float], url: str, *, external_id: str | None = None,
                  competition_origin: str = "page_heading") -> dict:
    home, away = home.strip(), away.strip()
    row = {"source": source, "sport": "football", "competition": competition,
           "home": home, "away": away, "kickoff_at": start, "observed_at": iso(instant(observed)),
           "market": "1x2_90m", "odds": dict(zip(KEYS, odds)), "url": safe_url(url),
           "source_event_id": external_id, "competition_origin": competition_origin,
           "parser_version": VERSION, "timezone_basis": "Europe/Moscow display time"}
    return validate_snapshot(row)


def validate_snapshot(row: dict) -> dict:
    if row.get("source") not in ("betboom", "winline") or row.get("sport") != "football" or row.get("market") != "1x2_90m":
        raise ValueError("Поддерживаются BetBoom/Winline, футбол, предматчевый исход 1X2 за основное время.")
    if any(not isinstance(row.get(field), str) or not 1 <= len(row[field]) <= 200 for field in ("home", "away", "competition")):
        raise ValueError("Некорректные команды или турнир.")
    if team_key(row["home"]) == team_key(row["away"]):
        raise ValueError("Команды должны различаться.")
    observed, start = instant(row["observed_at"]), instant(row["kickoff_at"])
    if start <= observed or start - observed > timedelta(days=400):
        raise ValueError("Нужен предматчевый снимок до начала встречи.")
    values = row.get("odds")
    if not isinstance(values, dict) or set(values) != set(KEYS) or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 1 < v <= 1000 for v in values.values()):
        raise ValueError("Нужны три конечных десятичных коэффициента больше 1.")
    address = urlsplit(row.get("url", ""))
    host = address.hostname or ""
    domain = row["source"] + ".ru"
    if address.scheme != "https" or (host != domain and not host.endswith("." + domain)):
        raise ValueError("Адрес источника не относится к букмекеру.")
    clean = {key: row.get(key) for key in ("source", "sport", "competition", "home", "away", "market", "source_event_id", "competition_origin", "parser_version", "timezone_basis")}
    clean.update(kickoff_at=iso(start), observed_at=iso(observed), odds={k: float(values[k]) for k in KEYS}, url=safe_url(row["url"]))
    identity = [clean["competition"].casefold(), team_key(clean["home"]), team_key(clean["away"]), clean["kickoff_at"]]
    clean["fixture_key"] = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]
    clean["event_key"] = clean["source"] + ":" + clean["fixture_key"]
    clean["id"] = hashlib.sha256(json.dumps([clean["event_key"], clean["observed_at"], clean["odds"]], sort_keys=True).encode()).hexdigest()
    return clean


def _teams_and_time(prefix: list[str], observed: str) -> tuple[str, str, str]:
    dates = [item for item in prefix if DATE_RE.fullmatch(item)]
    if len(dates) != 1:
        raise ValueError("Неоднозначное время начала.")
    teams = [item for item in prefix if item not in dates and not FORM_RE.fullmatch(item)
             and item not in ("Ещё", "/", "Футбол") and not re.fullmatch(r"\+\s*\d+", item)]
    if len(teams) != 2 or any(len(team) < 2 for team in teams):
        raise ValueError("Неоднозначные участники матча.")
    return teams[0], teams[1], kickoff(dates[0], observed)


def parse_betboom(record: dict) -> list[dict]:
    value = record["payload"].get("text", "").split("\nКупон", 1)[0]
    tokens = lines(value)
    url, observed = record["url"], record["observed_at"]
    path = urlsplit(url).path
    result = []
    if re.fullmatch(r"/sport/football/\d+/\d+/\d+/?", path):
        # Detail pages contain early-win and half-time markets too. Only this
        # exact labelled section represents the ordinary 90-minute 1X2.
        start_index = next((i for i in range(len(tokens) - 2) if tokens[i:i + 2] == ["Футбол", "/"]), None)
        if start_index is None:
            return []
        body = tokens[start_index + 2:]
        try:
            header = body[1:body.index("Все")]
            home, away, start = _teams_and_time(header, observed)
            text = "\n".join(body)
            match = re.search(r"(?m)^Исход\nОсновное время\n(П1\n\d+(?:[.,]\d+)?\n[XХ]\n\d+(?:[.,]\d+)?\nП2\n\d+(?:[.,]\d+)?)$", text)
            if match:
                prices = ODDS_RE.fullmatch(match[1])
                result.append(make_snapshot("betboom", body[0], home, away, start, observed,
                                            [float(v.replace(",", ".")) for v in prices.groups()], url,
                                            external_id=path.rstrip("/").split("/")[-1]))
        except (ValueError, IndexError):
            pass
        return result

    if re.fullmatch(r"/sport/football/\d+/\d+/?", path):
        candidates = [i for i in range(len(tokens) - 1) if tokens[i] == "Футбол" and "." in tokens[i + 1]]
        if not candidates:
            return []
        offset = candidates[-1]
        blocks = [(tokens[offset + 1], tokens[offset + 2:])]
    else:
        # Homepage: each football block has its own explicit sport + league.
        boundaries = [i for i, item in enumerate(tokens) if re.match(r"^(?:Футбол\.|Теннис\.|CS2\.|Хоккей\.|Баскетбол\.)", item)]
        blocks = []
        for position, index in enumerate(boundaries):
            if not tokens[index].startswith("Футбол."):
                continue
            end = boundaries[position + 1] if position + 1 < len(boundaries) else len(tokens)
            blocks.append((tokens[index].removeprefix("Футбол.").strip(), tokens[index + 1:end]))
    for competition, block in blocks:
        text = "\n".join(block)
        if re.search(r"(?mi)^(?:1-й тайм|2-й тайм|Досрочная победа)", text):
            continue
        previous = 0
        for match in ODDS_RE.finditer(text):
            prefix = text[previous:match.start()].strip().splitlines()
            previous = match.end()
            try:
                home, away, start = _teams_and_time(prefix, observed)
                result.append(make_snapshot("betboom", competition, home, away, start, observed,
                                            [float(v.replace(",", ".")) for v in match.groups()], url))
            except ValueError:
                continue
    return result


def _winline_table_card(record: dict, node: dict, teams: list[str], href: str, event_id: str) -> dict | None:
    context = node["winline_card"]
    if (not isinstance(context, dict) or not isinstance(context.get("text"), str)
            or not isinstance(context.get("tournament", ""), str)
            or not isinstance(context.get("datetimes", []), list)
            or any(not isinstance(value, str) for value in context.get("datetimes", []))):
        raise ValueError("Повреждён контекст карточки Winline.")
    tokens = lines(context["text"])
    if not any(tokens[index:index + 2] == teams for index in range(len(tokens) - 1)):
        return None
    if any(re.fullmatch(r"[12][ТT](?:\s*\d{1,3}(?:\+\d+)?['’]?)?", token, re.I)
           or token.casefold() in {"перерыв", "окончен", "завершён", "live"} for token in tokens):
        return None
    dates = {token for token in tokens if DATE_RE.fullmatch(token)}
    try:
        if len(dates) == 1:
            start = kickoff(dates.pop(), record["observed_at"])
        elif not dates and len(set(context.get("datetimes", []))) == 1:
            start = iso(instant(context["datetimes"][0]))
        else:
            return None
        # Other columns are handicap/total. A half never substitutes for Match.
        numbers = r"(\d+(?:[.,]\d+)?)"
        markets = re.findall(r"(?m)^Матч\n" + "\n".join([numbers] * 3) + r"$", "\n".join(tokens))
        if len(markets) != 1:
            return None
        competition = "Турнир не указан"
        parts = [value.strip() for value in context.get("tournament", "").split("|")]
        if len(parts) == 2 and all(parts) and all(len(value) <= 90 for value in parts):
            country, league = parts
            if country.casefold() == "англия" and league.casefold() in ("премьер-лига", "апл"):
                competition = EPL
            elif country.casefold() == "россия" and league.casefold() == "рпл":
                competition = "Россия. Премьер-лига"
            else:
                competition = country + ". " + league
        return make_snapshot("winline", competition, *teams, start, record["observed_at"],
                             [float(value.replace(",", ".")) for value in markets[0]], href,
                             external_id=event_id, competition_origin="winline_football_page" if competition == "Турнир не указан" else "winline_tournament_heading")
    except ValueError:
        return None


def parse_winline(record: dict, references: dict) -> list[dict]:
    text = record["payload"].get("text", "")
    page = urlsplit(record["url"])
    football_page = (page.scheme == "https" and page.hostname in ("winline.ru", "www.winline.ru")
                     and page.path.rstrip("/") == "/stavki/sport/futbol" and "Футбол" in lines(text))
    # The observed homepage mixes hockey, basketball and football. Restrict to
    # pre-match team links corroborated by an explicit football source, or a
    # football-specific page. Never infer sport from three prices alone.
    result = []
    for node in record["payload"].get("nodes", []):
        href = node.get("attrs", {}).get("href", "")
        link = urlsplit(href)
        match = re.fullmatch(r"/stavki/event/(\d+)/?", link.path) if link.scheme == "https" and link.hostname in ("winline.ru", "www.winline.ru") else None
        teams = lines(node.get("text", ""))
        if not match or len(teams) != 2:
            continue
        if "winline_card" in node:
            if football_page:
                row = _winline_table_card(record, node, teams, href, match[1])
                if row:
                    result.append(row)
            # A malformed/paused/in-play table must not fall through to a
            # coincidental triplet elsewhere in the whole-page text.
            continue
        if football_page:
            # The page's featured carousel includes other sports. Only events
            # scoped to its tournament table can prove football here.
            continue
        expression = r"(?m)^" + re.escape(teams[0]) + r"\s*\n" + re.escape(teams[1]) + r"\s*\n(\d+(?:[.,]\d+)?)\n(\d+(?:[.,]\d+)?)\n(\d+(?:[.,]\d+)?)\n([^\n]+)$"
        hits = list(re.finditer(expression, text))
        if len(hits) != 1:
            continue
        prices = [float(hits[0][k].replace(",", ".")) for k in (1, 2, 3)]
        try:
            start = kickoff(hits[0][4], record["observed_at"])
            reference = references.get((team_key(teams[0]), team_key(teams[1]), start))
            if not reference:
                continue
            competition = reference["competition"]
            origin = "corroborated_football_fixture"
            result.append(make_snapshot("winline", competition, teams[0], teams[1], start,
                                        record["observed_at"], prices, href,
                                        external_id=match[1], competition_origin=origin))
        except ValueError:
            continue
    return result


def normalize_capture(document: dict, football_references: list[dict] | None = None) -> dict:
    if not isinstance(document, dict) or document.get("format") != CAPTURE_FORMAT or not isinstance(document.get("records"), list):
        raise ValueError("Нужен файл захвата Matchlab.")
    if any(not isinstance(record, dict) for record in document["records"]):
        raise ValueError("Повреждён список записей захвата.")
    records = [r for r in document["records"] if r.get("kind") == "public_dom" and r.get("source") in ("betboom", "winline")]
    for record in records:
        if not isinstance(record.get("payload"), dict) or not isinstance(record["payload"].get("text"), str) or not isinstance(record.get("url"), str):
            raise ValueError("Повреждён публичный DOM-снимок.")
        instant(record.get("observed_at"))
        nodes = record["payload"].get("nodes", [])
        if not isinstance(nodes, list) or any(not isinstance(n, dict) or not isinstance(n.get("attrs", {}), dict) or not isinstance(n.get("text", ""), str) for n in nodes):
            raise ValueError("Повреждены DOM-атрибуты снимка.")
    rows, stats = [], Counter()
    for record in records:
        if record["source"] == "betboom":
            stats["betboom_dom_records"] += 1
            rows.extend(parse_betboom(record))
    references = {(team_key(r["home"]), team_key(r["away"]), r["kickoff_at"]): r for r in (football_references or []) if r.get("source") == "betboom" and r.get("sport") == "football"}
    references.update({(team_key(r["home"]), team_key(r["away"]), r["kickoff_at"]): r for r in rows})
    for record in records:
        if record["source"] == "winline":
            stats["winline_dom_records"] += 1
            rows.extend(parse_winline(record, references))
    unique = {row["id"]: row for row in rows}
    rows = sorted(unique.values(), key=lambda row: (row["observed_at"], row["source"], row["event_key"]))
    by_source = {}
    for source in ("betboom", "winline"):
        selected = [row for row in rows if row["source"] == source]
        by_source[source] = {"snapshots": len(selected), "events": len({r["event_key"] for r in selected})}
    return {"format": FORMAT, "parser_version": VERSION, "snapshots": rows,
            "report": {"sources": by_source, "dom_records": dict(stats),
                       "binary_feed_decoded": False, "market": "1x2_90m", "timezone": "Europe/Moscow",
                       "coverage": "visible pre-match cards only; not the entire bookmaker catalogue"}}


def load_snapshots(document: dict) -> dict:
    if not isinstance(document, dict):
        raise ValueError("Нужен JSON-объект захвата или нормализованных снимков.")
    if document.get("format") == CAPTURE_FORMAT:
        document = normalize_capture(document)
    if document.get("format") != FORMAT or not isinstance(document.get("snapshots"), list):
        raise ValueError("Нужен захват или нормализованные снимки Matchlab.")
    rows = [validate_snapshot(row) for row in document["snapshots"]]
    if not rows:
        raise ValueError("Ноль распознанных предматчевых футбольных исходов 1X2. Прежние данные сохранены.")
    return {"format": FORMAT, "parser_version": VERSION, "snapshots": rows, "report": document.get("report", {})}


def model_teams(row: dict) -> tuple[str, str] | None:
    if row.get("sport", "football") != "football" or row.get("event_kind", "match") != "match" or row["competition"] != EPL:
        return None
    home, away = ALIASES.get(row["home"]), ALIASES.get(row["away"])
    return (home, away) if home and away and home != away else None
