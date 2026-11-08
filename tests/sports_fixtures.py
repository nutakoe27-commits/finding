"""Synthetic market contracts; these documents are never production observations."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from matchlab.markets import FORMAT

PROJECTIONS = json.loads((Path(__file__).parent / 'fixtures/multisport_projection.json').read_text())


def document(item, now=None):
    now = now or datetime.now(timezone.utc)
    markets = [{'name':market['name'],'period':market['period'],
                'outcomes':[{'name':name,'odds':price} for name,price in market['outcomes']]} for market in item['markets']]
    return {'format':FORMAT,'source':'winline','sport':item['sport'],'sport_label':item['label'],'source_event_id':item['id'],
        'event_kind':'match' if item.get('home') else 'outright','home':item.get('home'),'away':item.get('away'),'title':item.get('title'),
        'competition':item['competition'],'url':f"https://winline.ru/stavki/sport/{item['slug']}/test/league/{item['id']}",
        'observed_at':now.isoformat(),'kickoff_at':(now+timedelta(days=1)).isoformat(),'phase':'prematch',
        'parser_version':'synthetic-multisport-contract','markets':markets,
        'coverage':{'expected_outcomes':sum(len(market['outcomes']) for market in markets),'all_tab_selected':True,'stable_passes':2,'scroll_exhausted':True}}
