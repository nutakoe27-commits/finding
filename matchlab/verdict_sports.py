"""Explicit sport/settlement contracts for qualitative neural verdicts.

No odds conversion or probability model lives here. A market must describe the
same contest and settlement scope as its eventual result.
"""
from dataclasses import dataclass
import re
import unicodedata


@dataclass(frozen=True)
class Profile:
    label: str
    archive_id: int
    scope: str
    market_label: str
    unit: str
    draw: bool = False


PROFILES = {
    'football': Profile('Футбол', 1, 'team_winner_90m', 'Победа в основное время', 'мячи', True),
    'hockey': Profile('Хоккей', 4, 'team_winner_regulation', 'Победа в основное время', 'шайбы', True),
    'basketball': Profile('Баскетбол', 2, 'match_winner_overtime', 'Победа в матче с овертаймом', 'очки'),
    'tennis': Profile('Теннис', 5, 'match_winner', 'Победа в матче', 'сеты'),
    'volleyball': Profile('Волейбол', 23, 'match_winner', 'Победа в матче', 'партии'),
    'table_tennis': Profile('Настольный теннис', 20, 'match_winner', 'Победа в матче', 'партии'),
}

ANALYSIS_FOCUS = {
    'hockey': 'Сравни результативность и оборону за основное время; не приписывай команде победу за три периода по победе в ОТ. Не выдумывай стартового вратаря и состав.',
    'basketball': 'Сравни последние результаты и набранные/пропущенные очки при сопоставимых условиях. Без числа владений не делай выводов об эффективности и темпе только из итогового счёта. Не выдумывай ротацию, травмы и усталость.',
    'tennis': 'Различай выигранные сеты, геймы и розыгрыши. Показатели первой и второй подачи относятся к разным ситуациям. Покрытие, личные встречи и формат используй только если они подтверждены фактами. Не переноси статистику игрока на другую пару.',
    'volleyball': 'Различай счёт по партиям и очки внутри партий. Учитывай результаты матчей и борьбу по партиям, если она показана. Не делай выводов о качестве приёма, блоке или составе по одному итоговому счёту.',
    'table_tennis': 'Различай партии и очки. Учитывай порядок недавних результатов и подтверждённый формат. Частые игры и короткие серии не доказывают усталость или устойчивое преимущество.',
}


def key(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).casefold().replace('ё', 'е').split())


def supported(event):
    from .sports import catalog_sport
    return event.get('sport') in PROFILES and catalog_sport(event.get('sport')) and event.get('event_kind') in (None, 'match')


def capabilities():
    return {sport: {'label': p.label, 'scope': p.scope, 'market_label': p.market_label,
                    'draw_possible': p.draw} for sport, p in PROFILES.items() if supported({'sport':sport})}


def market_matches(market, sport):
    """Conservative name AND period AND outcome recognition, never price inference."""
    p = PROFILES.get(sport)
    if not p or market.get('is_stale') or market.get('parameter') not in (None, ''):
        return False
    outcomes = market.get('outcomes') or []
    if market.get('outcomes_complete') is False or any(o.get('parameter') not in (None, '') for o in outcomes):
        return False
    name, period = key(market.get('name')), key(market.get('period'))
    wording = name + ' ' + period
    if re.search(r'тайм|период|четверт|сет|гейм|партия|парти[ию]|раунд|половин|half|quarter|period|set|game|фора|тотал|двойной|проход|выход|серии|серия|четност|углов|карточ', wording):
        return False
    labels = {key(o.get('name')) for o in outcomes}
    required = ({'п1', 'x', 'п2'}, {'п1', 'х', 'п2'}, {'1', 'x', '2'}, {'1', 'х', '2'}) if p.draw else ({'п1', 'п2'}, {'1', '2'})
    if labels not in required or len(outcomes) != (3 if p.draw else 2):
        return False
    if p.draw:
        return bool(re.search(r'1\s*[xх]\s*2', name)) and not re.search(r'овертайм|буллит|доп\.?\s*врем|(?<!\w)от(?!\w)|с учет', wording)
    if not re.search(r'победитель|победа в матче|исход\s*12|match winner|moneyline', name):
        return False
    if sport == 'basketball':
        # A bare two-way market is not proof that overtime is included.
        return bool(re.search(r'с\s+(?:учетом\s+)?(?:от\b|овертайм)|including overtime', wording))
    return not re.search(r'овертайм|буллит|(?<!\w)от(?!\w)', wording)


def prices_valid(prices, sport):
    import math
    p = PROFILES.get(sport)
    expected = {'home', 'draw', 'away'} if p and p.draw else {'home', 'away'}
    return bool(p and isinstance(prices, dict) and set(prices) == expected and
                all(type(v) in (int, float) and math.isfinite(v) and 1 < v <= 100000 for v in prices.values()))


def result_score(value, sport):
    """Return the score in the profile's scope, or abstain on unknown formats.

    Archive rows are completed observations. Text signalling retirement,
    cancellation or a technical result is deliberately not accepted. Parenthetic
    period scores must corroborate the total; they are not discarded blindly.
    """
    if sport == 'football':
        from .chances import football_result
        parsed = football_result(value)
        return parsed['full'] if parsed else None
    p = PROFILES.get(sport)
    if not p:
        return None
    m = re.fullmatch(r'\s*(\d{1,3})\s*:\s*(\d{1,3})(?:\s*\(\s*([\d\s:,;]+)\s*\))?\s*', str(value or ''))
    if not m:
        return None
    full = int(m[1]), int(m[2])
    parts = []
    if m[3]:
        pieces = re.findall(r'(\d{1,3})\s*:\s*(\d{1,3})', m[3])
        if re.sub(r'\d{1,3}\s*:\s*\d{1,3}|[\s,;]', '', m[3]):
            return None
        parts = [tuple(map(int, pair)) for pair in pieces]
    if sport in ('tennis', 'volleyball', 'table_tennis'):
        targets = {'tennis': (2, 3), 'volleyball': (3,), 'table_tennis': (2, 3, 4)}[sport]
        if max(full) not in targets or min(full) >= max(full):
            return None
        if parts:
            if any(h == a for h, a in parts) or len(parts) != sum(full):
                return None
            wins = sum(h > a for h, a in parts), sum(a > h for h, a in parts)
            if wins != full:
                return None
            for i, (h, a) in enumerate(parts):
                high, low = max(h, a), min(h, a)
                if sport == 'tennis':
                    complete = ((high == 6 and low <= 4) or (high == 7 and low in (5, 6)) or
                                (high >= 8 and high-low == 2) or (i == len(parts)-1 and high >= 10 and high-low >= 2))
                else:
                    target = 11 if sport == 'table_tennis' else 15 if i == 4 else 25
                    complete = (high == target and low <= target-2) or (high > target and high-low == 2)
                if not complete:
                    return None
        return full
    if sport == 'basketball':
        if full[0] == full[1] or not 1 <= max(full) <= 300:
            return None
        if parts and (not 2 <= len(parts) <= 10 or tuple(map(sum, zip(*parts))) != full):
            return None
        return full
    if sport == 'hockey':
        if max(full) > 30:
            return None
        if not parts:
            # A bare final pair cannot distinguish regulation from overtime.
            return None
        if not 3 <= len(parts) <= 5 or tuple(map(sum, zip(*parts))) != full:
            return None
        regulation = tuple(map(sum, zip(*parts[:3])))
        if len(parts) > 3 and regulation[0] != regulation[1]:
            return None
        return regulation
    return None
