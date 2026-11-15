"""Sport identity and collection profiles; these are not trained models."""

from collections import deque
import re

PROFILES = {
    "football": {"label":"Футбол", "score_units":["goals"], "periods":"halves",
                 "model_features":["team_form","goals","shots","corners","cards","lineups"]},
    "hockey": {"label":"Хоккей", "score_units":["goals"], "periods":"periods",
               "model_features":["team_form","shots","saves","power_play","penalty_minutes","lineups"]},
    "tennis": {"label":"Теннис", "score_units":["sets","games"], "periods":"sets",
               "model_features":["player_form","surface","serve","return","break_points","head_to_head"]},
    "basketball": {"label":"Баскетбол", "score_units":["points"], "periods":"quarters",
                   "model_features":["team_form","points","rebounds","assists","turnovers","lineups"]},
    "volleyball": {"label":"Волейбол", "score_units":["sets","points"], "periods":"sets",
                   "model_features":["team_form","sets","points","attack","blocks","serve"]},
}
DEFAULT_SPORTS = ",".join(PROFILES)
ALIASES = {key:key for key in PROFILES} | {p["label"].casefold():key for key,p in PROFILES.items()} | {
    "soccer":"football", "icehockey":"hockey", "ice hockey":"hockey"}


def selection(value):
    if value == "all":
        return None
    values = tuple(dict.fromkeys(x.strip() for x in value.split(",") if x.strip()))
    if not values or any(not re.fullmatch(r"[a-z][a-z0-9_]{0,49}", x) for x in values):
        raise ValueError("Specify sport keys separated by commas, or all")
    return values


def accepts(value, key):
    chosen = selection(value)
    return chosen is None or key in chosen


def balanced(events, limit):
    """Round-robin sports; preserve event priority within each sport."""
    groups = {}
    for event in events:
        groups.setdefault(event["sport"], deque()).append(event)
    output = []
    while groups and len(output) < limit:
        for key in list(groups):
            output.append(groups[key].popleft())
            if not groups[key]:
                del groups[key]
            if len(output) == limit:
                break
    return output


def provider_sport(match, name=None):
    for value in (name, match.get("_doctype")):
        if isinstance(value, str) and value.casefold() in ALIASES:
            return ALIASES[value.casefold()]
    return None
