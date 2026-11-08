"""Bounded, auditable context for an AI explanation of a bookmaker snapshot."""
from datetime import datetime, timezone
import hashlib
import json

from .bookmakers import instant
from .markets import main_odds


def analysis_context(document: dict, *, market_key=None, outcome_key=None, notes="", statistics=None) -> dict:
    selected = next((market for market in document["markets"] if market["key"] == market_key), None) if market_key else None
    if market_key and not selected:
        raise ValueError("Выбранный рынок отсутствует в текущем снимке. Обновите матч.")
    outcome = next((item for item in selected["outcomes"] if item["key"] == outcome_key), None) if selected and outcome_key else None
    if outcome_key and not outcome:
        raise ValueError("Выбранный исход отсутствует в текущем снимке. Выберите его заново.")
    if outcome and outcome["state"] != "open":
        raise ValueError("Коэффициент выбранного исхода сейчас недоступен.")
    priority = ("Исход 1X2", "Двойной шанс", "Тотал (осн.время)", "Обе забьют", "Фора")
    ordered = sorted(document["markets"], key=lambda market: (
        0 if selected and market["key"] == selected["key"] else 1,
        priority.index(market["name"]) if market["name"] in priority else len(priority)))
    markets, count = [], 0
    for market in ordered:
        if len(markets) >= 12 or count >= 80:
            break
        outcomes = market["outcomes"][:80 - count]
        # Always retain an explicitly selected outcome even in a large table.
        if outcome and selected["key"] == market["key"] and outcome not in outcomes:
            outcomes = [outcome, *outcomes[:-1]]
        markets.append({key: market.get(key) for key in ("key", "name", "group", "period", "parameter")} | {
            "outcomes": [{key: item.get(key) for key in ("key", "name", "parameter", "odds", "state")} for item in outcomes]})
        count += len(outcomes)
    current = datetime.now(timezone.utc)
    age = max(0, int((current - instant(document["capture_started_at"])).total_seconds()))
    prices = main_odds(document)
    total = sum(1 / value for value in prices.values()) if prices else None
    return {"event": {key: document.get(key) for key in ("source_event_id", "home", "away", "competition", "phase", "kickoff_at", "url")},
            "request_time": {"at": current.isoformat(), "kickoff_passed": bool(document.get("kickoff_at") and instant(document["kickoff_at"]) <= current)},
            "snapshot": {"observed_at": document["observed_at"], "capture_started_at": document["capture_started_at"],
                         "age_seconds": age, "is_stale": age > 180, "complete": document["coverage"]["complete"]},
            "selection": {"market_key": market_key, "outcome_key": outcome_key},
            "markets": markets,
            "coverage": {"analyzed_markets": len(markets), "analyzed_outcomes": count,
                         "stored_markets": len(document["markets"]), "stored_outcomes": document["coverage"]["captured_outcomes"]},
            "market_implied": {"probabilities": {key: (1 / value) / total for key, value in prices.items()},
                               "overround": total - 1, "basis": "regular_time_1x2_bookmaker_prices"} if prices else None,
            "statistical_model": statistics, "unverified_user_notes": notes,
            "missing_data": ["current_lineups", "injuries", "news", "current_score_and_match_clock"]}


def request_key(document: dict, market_key, outcome_key, notes: str) -> str:
    current = datetime.now(timezone.utc)
    stale = (current - instant(document["capture_started_at"])).total_seconds() > 180
    started = bool(document.get("kickoff_at") and instant(document["kickoff_at"]) <= current)
    value = [document["source_event_id"], document["observed_at"], document["capture_started_at"],
             document["markets"], market_key, outcome_key, notes, stale, started]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
