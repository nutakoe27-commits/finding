"""The observed Winline homepage selection, without a local editorial ranking."""
from datetime import datetime, timezone
import re

from .bookmakers import instant


def select(events, *, limit=100, now=None):
    current = now or datetime.now(timezone.utc)
    candidates = {}
    for event in events:
        rank = event.get('featured_rank')
        if type(rank) is not int or not 0 <= rank < 1000:
            continue
        if not re.fullmatch(r'\d{1,20}', str(event.get('source_event_id', ''))):
            continue
        if not event.get('home') or not event.get('away') or event.get('event_kind', 'match') != 'match':
            continue
        if event.get('phase') not in ('prematch', 'live'):
            continue
        try:
            if event['phase'] == 'prematch' and (not event.get('kickoff_at') or instant(event['kickoff_at']) <= current):
                continue
        except (ValueError, TypeError):
            continue
        key = (event.get('source_kind', 'legacy'), event['source_event_id'])
        if key not in candidates or rank < candidates[key]['featured_rank']:
            candidates[key] = event
    return sorted(candidates.values(), key=lambda event: event['featured_rank'])[:limit]


def collection(repo, *, sport='', phase='', limit=100):
    from . import archive
    # Old DOM imports carry no observed homepage selection. Do not substitute
    # all matches, nearby fixtures or a favourite league for missing source data.
    if not archive.overview(repo)['available']:
        return {'format': 'matchlab.featured-events.v1', 'events': [],
                'selection': 'winline_homepage', 'source_kind': 'stream'}
    data = archive.catalog(repo, include_started=True, sport=sport, phase=phase,
                           limit=10000, offset=0, sort='kickoff')
    return {'format': 'matchlab.featured-events.v1',
            'events': select(data['events'], limit=limit),
            'selection': 'winline_homepage', 'source_kind': 'stream'}
