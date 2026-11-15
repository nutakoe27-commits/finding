"""Price-blind feature allowlist and a separately applied, frozen paper policy."""
import math

PROFILE='statistics_only.v1'
CATEGORICAL=['sport','competition','selection_side','score_units','market_scope','capture_state']
NUMERIC=[side+'_'+name for side in ('selected','opponent') for name in
    ('form_count','form_win_rate','season_played','season_win_rate','goals_for_per_match','goals_against_per_match')]
SCHEMA={'format':'doscbot.statistics-schema.v1','target':'participant_win.v1','feature_profile':PROFILE,
    'categorical':CATEGORICAL,'numeric':NUMERIC,'missing_indicators':[k+'__missing' for k in NUMERIC],
    'feature_policy':'Only participant statistics and sport/competition/scope/capture context. No prices, implied probabilities, margin, market count or their missing indicators.'}
NAMES=CATEGORICAL+NUMERIC+SCHEMA['missing_indicators']
DEFAULT_POLICY={'version':'statistics-price-gate.v1','min_probability':0.55,'min_expected_return':0.05}


def project(features):
    """Accept an existing verified export row, return ONLY the price-blind contract."""
    result={k:features[k] for k in CATEGORICAL+NUMERIC}
    for k in CATEGORICAL:
        if not isinstance(result[k],str):raise ValueError('Invalid statistics category')
    for k in NUMERIC:
        v=result[k]
        if v is not None and (type(v) not in (int,float) or not math.isfinite(v)):
            raise ValueError('Invalid statistics number')
        result[k+'__missing']=int(v is None)
    return result


def supported(features):
    return all(any(type(features.get(side+'_'+key)) in (int,float) and features[side+'_'+key]>0
                   for key in ('form_count','season_played')) for side in ('selected','opponent'))


def vector(features):
    if set(features)!=set(NAMES) or features!=project(features):raise ValueError('Exact statistics-only features required')
    return [features[k] if features[k] is not None else float('nan') for k in NAMES]


def validate_policy(policy):
    if (set(policy)!=set(DEFAULT_POLICY) or policy['version']!=DEFAULT_POLICY['version'] or
        type(policy['min_probability']) not in (int,float) or not .5<=policy['min_probability']<=1 or
        type(policy['min_expected_return']) not in (int,float) or not 0<=policy['min_expected_return']<=1):
        raise ValueError('Invalid frozen paper policy')
    return policy


def decide(probabilities,prices,policy=None):
    policy=validate_policy(dict(DEFAULT_POLICY if policy is None else policy))
    if set(probabilities)!={'home','away'} or any(type(p) not in (int,float) or not math.isfinite(p) or not 0<=p<=1 for p in probabilities.values()):
        raise ValueError('Invalid participant probabilities')
    # Stage A cannot access prices. No conversion from bookmaker odds is performed.
    side=None if abs(probabilities['home']-probabilities['away'])<1e-12 else max(probabilities,key=probabilities.get)
    a={'version':'statistics-winner.v1','side':side,'probability':probabilities[side] if side else None}
    b={'version':policy['version'],'side':side,'decision':'skip','odds':None,'expected_return':None,'reason':'no_preference','policy':policy}
    if side:
        p=a['probability'];price=prices.get(side)
        if type(price) not in (int,float) or not math.isfinite(price) or price<=1:
            b['reason']='missing_price'
        else:
            edge=p*price-1
            b.update(odds=price,expected_return=edge,reason='probability_below_threshold' if p<policy['min_probability'] else 'price_below_threshold' if edge<policy['min_expected_return'] else 'paper_rule_passed')
            if b['reason']=='paper_rule_passed':b['decision']='bet'
    return {'statistics':a,'statistics_price':b}
