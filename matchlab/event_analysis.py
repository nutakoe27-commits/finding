"""Complete, auditable market context for an AI explanation of a bookmaker snapshot."""
from datetime import datetime, timezone
import hashlib
import json

from .bookmakers import instant
from .freshness import quote_ttl

ANALYSIS_VERSION = 'independent-history-analysis-v17'

def analysis_context(document: dict, *, market_key=None, outcome_key=None, notes="", statistics=None, team_statistics=None,archive_history=None) -> dict:
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
        outcomes = market["outcomes"]
        markets.append({key: market.get(key) for key in ("key", "name", "group", "period", "parameter", "variant_id", "observed_at", "is_stale")} | {
            "outcomes_complete": len(outcomes) == len(market["outcomes"]),
            "outcomes": [{key: item.get(key) for key in ("key", "name", "parameter", "state")} for item in outcomes]})
        count += len(outcomes)
    current = datetime.now(timezone.utc)
    age = max(0, int((current - instant(document["capture_started_at"])).total_seconds()))
    return {"event": {key: document.get(key) for key in ("source_event_id", "source_kind", "sport", "sport_label", "event_kind", "title", "home", "away", "competition", "phase", "kickoff_at", "url", "live_score", "live_clock", "period_scores", "red_card_presence", "final_result", "source_present")},
            "request_time": {"at": current.isoformat(), "kickoff_passed": bool(document.get("kickoff_at") and instant(document["kickoff_at"]) <= current)},
            "snapshot": {"observed_at": document["observed_at"], "capture_started_at": document["capture_started_at"], "content_version":document.get("content_version"),
                         "age_seconds": age, "is_stale": age > quote_ttl(document) or document.get("source_present") is False, "complete": document["coverage"]["complete"]},
            "selection": {"market_key": market_key, "outcome_key": outcome_key},
            "markets": markets,
            "coverage": {"analyzed_markets": len(markets), "analyzed_outcomes": count,
                         "stored_markets": len(document["markets"]), "stored_outcomes": document["coverage"]["captured_outcomes"]},
            "market_implied": None,
            "statistical_model": None,
            "team_statistics":team_statistics, "archive_history":archive_history, "unverified_user_notes": notes,
            "missing_data": ["current_lineups" if document.get('sport','football') == 'football' else "participants_status", "injuries", "news"] + (["current_score_and_match_clock"] if not document.get("live_score") and not document.get("live_clock") else [])}


def request_key(document: dict, market_key, outcome_key, notes: str, team_statistics=None,archive_history=None) -> str:
    current = datetime.now(timezone.utc)
    stale = (current - instant(document["capture_started_at"])).total_seconds() > quote_ttl(document) or document.get("source_present") is False
    started = bool(document.get("kickoff_at") and instant(document["kickoff_at"]) <= current)
    value = [ANALYSIS_VERSION, document["source_event_id"], document.get('sport','football'), document.get("content_version",document["observed_at"]), None if document.get("source_kind")=="stream" else document["capture_started_at"],
             {key:document.get(key) for key in ('home','away','title','event_kind','competition','kickoff_at','coverage')},
             [{k:v for k,v in m.items() if k not in ("observed_at","age_seconds")} for m in document["markets"]] if document.get("source_kind")=="stream" else document["markets"], market_key, outcome_key, notes, stale, started,
             {key:team_statistics.get(key) for key in ('statistics_id','observed_at','match_phase','is_stale','capture_kind')} if team_statistics else None]
    value.append(archive_history.get('revision') if archive_history else None)
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
