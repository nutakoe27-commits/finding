"""Validated event-market documents. No browser or scientific dependencies."""
from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import math
import re
from urllib.parse import urlsplit

from .bookmakers import instant, iso
from .capture import safe_url
from .winline_browser import event_id_from_url, statistics_id_from_url
from .sports import ordinary_sport, sport_from_url, sport_label

FORMAT = "matchlab.event-markets.v1"


def label(value, maximum=300, *, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("Некорректная подпись рынка, исхода или события.")
    return value.strip()


def identity(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]


def main_odds(document: dict) -> dict | None:
    """A compact display of explicit regular-time 1X2; never infer a market."""
    candidates = []
    names = {"П1": "home", "1": "home", "X": "draw", "Х": "draw", "П2": "away", "2": "away"}
    for market in document.get("markets", []):
        if not re.fullmatch(r"Исход\s+1[XХ]2", market["name"], re.I) or market.get("parameter"):
            continue
        period = re.sub(r"[.\s]", "", market.get("period") or "").casefold()
        if period not in ("оснвремя", "основноевремя"):
            continue
        prices = {names[outcome["name"].upper()]: outcome["odds"]
                  for outcome in market["outcomes"] if outcome["name"].upper() in names and outcome["state"] == "open"}
        if len(market["outcomes"]) == 3 and set(prices) == {"home", "draw", "away"}:
            candidates.append(prices)
    return candidates[0] if len(candidates) == 1 else None


def headline_market(document: dict) -> dict | None:
    """Pick an explicit match-winner market; preserve its labels and scope."""
    winners = []
    for market in document.get("markets", []):
        name, period = market["name"], market.get("period") or ""
        if not re.fullmatch(r"(?:Исход(?:\s+(?:1[XХ]2|12))?|Победитель(?:\s+матча)?|Победа(?:\s+в\s+матче)?|Победит)(?:\s*\([^)]*\))?", name, re.I):
            continue
        if market.get("parameter") or re.search(r"\b(?:тайм|четверт\w*|период|сет|гейм|раунд|карта|мин|партия|иннинг)\d*\b", period + " " + name, re.I):
            continue
        labels = [item["name"].casefold() for item in market["outcomes"]]
        if len(labels) < 2 or len(labels) != len(set(labels)):
            continue
        rank = 0 if re.sub(r"[.\s]", "", period).lower() in ("оснвремя", "основноевремя") else 1 if period.lower() == "матч" else 2
        winners.append((rank, market))
    if not winners:
        return None
    winners.sort(key=lambda item: item[0])
    rank, chosen = winners[0]
    if len([item for item in winners if item[0] == rank]) != 1:
        return None
    return chosen


def headline_preview(document: dict) -> dict | None:
    market = headline_market(document)
    if not market:
        return None
    return {key: market.get(key) for key in ("key", "name", "period", "parameter")} | {
        "outcomes": market["outcomes"][:3], "outcome_count": len(market["outcomes"])}


def validate_event_markets(value: dict) -> dict:
    if not isinstance(value, dict) or value.get("format") != FORMAT or value.get("source") != "winline" or not ordinary_sport(value.get("sport")):
        raise ValueError("Нужен документ спортивных рынков Winline.")
    sport = value["sport"]
    event_id = value.get("source_event_id")
    if not isinstance(event_id, str) or not re.fullmatch(r"\d{1,20}", event_id):
        raise ValueError("Некорректный ID события.")
    if event_id_from_url(value.get("url", "")) != event_id:
        raise ValueError("Страница не соответствует событию Winline.")
    url_sport = sport_from_url(value["url"])
    if (url_sport and url_sport != sport) or (not url_sport and sport != "football"):
        raise ValueError("Вид спорта не соответствует разделу источника.")
    observed = instant(value.get("observed_at"))
    capture_started = instant(value.get("capture_started_at", value.get("observed_at")))
    if capture_started > observed or observed - capture_started > timedelta(hours=1):
        raise ValueError("Некорректный интервал наблюдения.")
    phase = value.get("phase", "unknown")
    if phase not in ("prematch", "live", "finished", "unknown"):
        raise ValueError("Некорректная фаза события.")
    start = instant(value["kickoff_at"]) if value.get("kickoff_at") else None
    if start and (start - observed > timedelta(days=400) or (phase == "prematch" and start <= observed)):
        raise ValueError("Некорректное предматчевое время.")
    kind = value.get("event_kind", "match")
    if kind not in ("match", "outright"):
        raise ValueError("Некорректный тип события.")
    result = {"format": FORMAT, "source": "winline", "sport": sport, "source_event_id": event_id,
              "sport_label": label(sport_label(sport, value.get("sport_label")), 100), "event_kind": kind,
              "title": label(value.get("title"), 300, optional=kind == "match"),
              "url": safe_url(value["url"]), "home": label(value.get("home"), 200, optional=kind == "outright"), "away": label(value.get("away"), 200, optional=kind == "outright"),
              "competition": label(value.get("competition", "Турнир не указан"), 200),
              "phase": phase, "kickoff_at": iso(start) if start else None, "observed_at": iso(observed),
              "capture_started_at": iso(capture_started),
              "parser_version": label(value.get("parser_version"), 80)}
    stats_url = value.get('statistics_url')
    stats_status = value.get('statistics_link_status', 'found' if stats_url else 'missing')
    if stats_status not in ('found', 'missing', 'ambiguous') or (bool(stats_url) != (stats_status == 'found')):
        raise ValueError('Некорректная связь со статистикой события.')
    stats_id = statistics_id_from_url(stats_url) if isinstance(stats_url, str) else None
    if stats_url and not stats_id:
        raise ValueError('Некорректный адрес статистики события.')
    result.update(statistics_url='https://winline.ru/stats/match/' + stats_id if stats_id else None,
                  statistics_link_status=stats_status)
    if kind == "match" and result["home"] == result["away"]:
        raise ValueError("Нужны разные участники события.")
    incoming = value.get("markets")
    if not isinstance(incoming, list) or not 1 <= len(incoming) <= 10000:
        raise ValueError("Нужен непустой ограниченный список распознанных рынков.")
    markets = {}
    for item in incoming:
        if not isinstance(item, dict):
            raise ValueError("Повреждён рынок.")
        market = {"name": label(item.get("name")), "group": label(item.get("group"), 200, optional=True),
                  "period": label(item.get("period"), 100, optional=True), "parameter": label(item.get("parameter"), 100, optional=True),
                  "source_market_id": label(item.get("source_market_id"), 100, optional=True)}
        variant_id=item.get('variant_id')
        if variant_id is not None:
            if not isinstance(variant_id,str) or not re.fullmatch(r'[a-f0-9]{32}',variant_id):
                raise ValueError('Некорректный идентификатор набора исходов.')
            market['variant_id']=variant_id
        market["key"] = identity(market)
        selections = item.get("outcomes")
        if not isinstance(selections, list) or not 1 <= len(selections) <= 2000:
            raise ValueError("Нужны исходы каждого рынка.")
        outcomes = {}
        for selection in selections:
            if not isinstance(selection, dict):
                raise ValueError("Повреждён исход.")
            price = selection.get("odds")
            state = selection.get("state", "open" if price is not None else "unavailable")
            if not isinstance(state, str) or state not in ("open", "suspended", "unavailable"):
                raise ValueError("Некорректное состояние исхода.")
            if price is None:
                if state == "open":
                    raise ValueError("Доступный исход должен иметь коэффициент.")
            elif isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or not 1 <= price <= 1000000:
                raise ValueError("Некорректный коэффициент исхода.")
            outcome = {"name": label(selection.get("name")), "parameter": label(selection.get("parameter"), 100, optional=True),
                       "source_outcome_id": label(selection.get("source_outcome_id"), 100, optional=True)}
            outcome["key"] = identity(outcome)
            outcome.update(odds=float(price) if price is not None else None, state=state)
            previous = outcomes.get(outcome["key"])
            if previous and previous != outcome:
                raise ValueError("Противоречивые дубли исхода.")
            outcomes[outcome["key"]] = outcome
        market["outcomes"] = list(outcomes.values())
        previous = markets.get(market["key"])
        if previous and previous != market:
            raise ValueError("Противоречивые дубли рынка.")
        markets[market["key"]] = market
    result["markets"] = list(markets.values())
    coverage = value.get("coverage", {})
    if not isinstance(coverage, dict):
        raise ValueError("Некорректное описание охвата.")
    expected = coverage.get("expected_markets")
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int) or not 1 <= expected <= 10000):
        raise ValueError("Некорректный счётчик рынков страницы.")
    stable = coverage.get("stable_passes", 0)
    if isinstance(stable, bool) or not isinstance(stable, int) or not 0 <= stable <= 100:
        raise ValueError("Некорректный счётчик проходов страницы.")
    hidden = coverage.get("hidden_groups", 0)
    if isinstance(hidden, bool) or not isinstance(hidden, int) or not 0 <= hidden <= 10000:
        raise ValueError("Некорректный счётчик свёрнутых групп.")
    exhausted = coverage.get("scroll_exhausted", False) is True
    expected_outcomes = coverage.get("expected_outcomes")
    unparsed = coverage.get("unparsed_outcomes", 0)
    for count in (expected_outcomes, unparsed):
        if count is not None and (isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 100000):
            raise ValueError("Некорректный счётчик исходов страницы.")
    captured_outcomes = sum(len(market["outcomes"]) for market in markets.values())
    all_selected = coverage.get("all_tab_selected", False) is True
    accounted = (expected == len(markets) if expected is not None else
                 expected_outcomes is not None and expected_outcomes > 0 and expected_outcomes == captured_outcomes and all_selected)
    result["coverage"] = {"expected_markets": expected, "captured_markets": len(markets),
                          "expected_outcomes": expected_outcomes, "captured_outcomes": captured_outcomes,
                          "unparsed_outcomes": unparsed, "all_tab_selected": all_selected,
                          "stable_passes": stable, "hidden_groups": hidden, "scroll_exhausted": exhausted,
                          "complete": accounted and unparsed == 0 and stable >= 2 and hidden == 0 and exhausted}
    return result
