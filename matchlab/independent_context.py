"""Exclude bookmaker prices and implied estimates at the AI boundary."""
import re

PRICE_FIELD=re.compile(r'^(?:odds|prices?|koef|coef|coefficients?|bookmaker_probabilities|implied_probabilities|market_implied|overround|reciprocal_sum|margin|statistical_model)$',re.I)


def without_prices(value):
    if isinstance(value,dict):return {k:without_prices(v) for k,v in value.items() if not PRICE_FIELD.fullmatch(k)}
    if isinstance(value,list):return [without_prices(v) for v in value]
    return value


def independent_context(context):
    result=without_prices(context)
    result['market_implied']=None
    result['statistical_model']=None
    assessment=result.get('probability_assessment')
    if isinstance(assessment,dict):
        assessment['markets']=[r for r in assessment.get('markets',[]) if r.get('basis')=='history']
        if (assessment.get('winner') or {}).get('basis')!='history':assessment['winner']=None
        assessment['facts']=[f for f in assessment.get('facts',[]) if not re.search(r'по котировкам|по линии|коэффициент',f,re.I)]
        assessment['independent_only']=True
    return result
