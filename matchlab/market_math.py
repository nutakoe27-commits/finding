"""Calculations that describe a captured, complete 1X2 market."""
import math
import re
import unicodedata


def normalized_1x2(market):
    """Return proportional market weights, or None for ambiguous/incomplete data.

    These weights allocate the observed overround proportionally. They are not
    an independent probability estimate or proof of the bookmaker's true margin.
    """
    if not isinstance(market, dict) or market.get('outcomes_complete') is not True:
        return None
    name = unicodedata.normalize('NFKC', str(market.get('name') or '')).casefold()
    if not re.search(r'(?<!\w)1\s*[xх]\s*2(?!\w)', name):
        return None
    if re.search(r'(?<![а-яё])фор[а-яё]*|гандикап|handicap|spread|азиат', name):
        return None
    if market.get('parameter') not in (None, ''):
        return None
    outcomes = market.get('outcomes')
    if not isinstance(outcomes, list) or len(outcomes) != 3:
        return None
    aliases = {'1': '1', 'п1': '1', 'х': 'X', 'x': 'X', '2': '2', 'п2': '2'}
    odds = {}
    for item in outcomes:
        if not isinstance(item, dict) or item.get('state') != 'open' or item.get('parameter') not in (None, ''):
            return None
        name = unicodedata.normalize('NFKC', str(item.get('name') or '')).strip().casefold()
        key = aliases.get(name)
        value = item.get('odds')
        if (key is None or key in odds or isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 1):
            return None
        odds[key] = value
    if set(odds) != {'1', 'X', '2'}:
        return None
    total = sum(1 / odds[key] for key in ('1', 'X', '2'))
    return {'weights': {key: (1 / odds[key]) / total for key in ('1', 'X', '2')},
            'overround': total - 1, 'reciprocal_sum': total}
