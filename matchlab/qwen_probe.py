"""Two sequential, read-only model probes. No source content or credentials in output."""
from dataclasses import replace
import json
import sqlite3
import time
import zlib
from urllib.parse import quote

from . import qwen
from .independent_context import independent_context
from .event_reasoning import Budget,EXTRACT,FACT_EXTRACT,direct_system,trusted_facts,records_for,record_limit,encoded,evidence_payload


def saved_sample(settings,full=False):
    from .mysql_backend import readonly,MySQLTarget
    if not isinstance(settings.database,MySQLTarget) and not settings.database.is_file():return None,None
    with readonly(settings.database) as db:
        audit=bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_analysis_inputs'").fetchone())
        query=('SELECT a.id,a.generated_at,a.payload,i.payload FROM event_analyses a LEFT JOIN event_analysis_inputs i ON i.analysis_id=a.id '
               if audit else 'SELECT id,generated_at,payload,NULL FROM event_analyses ')
        for row in db.execute(query+'ORDER BY generated_at DESC LIMIT 50'):
            context=json.loads(row[2]).get('context') or {};source='saved_report'
            if full:
                if row[3]:context=json.loads(zlib.decompress(row[3]));source='audit_snapshot'
                elif context.get('market_selection'):continue
            context={key:value for key,value in context.items() if key!='ai_generation'}
            # Probes mirror new match requests; archived search snippets are not resent.
            context['web_research']={'state':'disabled','sources':[]}
            return context,{'analysis_id':row[0],'generated_at':row[1],'source':source}
    return None,None


def saved_context(settings):
    value=saved_sample(settings)[0]
    return independent_context(value) if value is not None else None


def compare_context(settings,market_limit=None):
    """Tokenize both views of the same saved snapshot; never generate or mutate data."""
    from .market_focus import focus_context
    budget=Budget(settings,qwen._request)
    limit=settings.qwen_market_limit if market_limit is None else market_limit
    result={'model':settings.qwen_model,'context_tokens':budget.limit,'market_limit':limit}
    full,sample=saved_sample(settings,True)
    if full is None:return result|{'sample':'no_full_saved_analysis'}
    result['sample']=sample
    full=independent_context(full)
    for label,context in (('full',full),('focused',focus_context(full,limit))):
        records=records_for(context,limit=record_limit(budget))
        payload=evidence_payload(context,records)
        messages=[{'role':'system','content':direct_system(context)},{'role':'user','content':encoded(payload)}]
        item={'markets':len(context.get('markets',[])),
              'outcomes':sum(len(m.get('outcomes',[])) for m in context.get('markets',[])),
              'assessment_outcomes':len((context.get('probability_assessment') or {}).get('markets',[])),
              'input_bytes':sum(len(m['content'].encode()) for m in messages),
              'sections_bytes':{key:len(encoded(context.get(key)).encode()) for key in ('markets','probability_assessment','team_statistics','archive_history')}}
        try:
            reply=qwen._request(replace(budget.token_settings,qwen_timeout=min(10,settings.qwen_timeout)),'post','/tokenize',
                                json={'model':settings.qwen_model,'messages':messages,'add_generation_prompt':True})
            count=reply.get('count')
            if type(count) is not int or count<0:raise ValueError('Invalid tokenizer count')
            required=count+min(settings.qwen_max_tokens,1536 if trusted_facts(context) else 512)+256
            item.update(input_tokens=count,minimum_context_tokens=required,fits_advertised_window=required<=budget.limit,
                        fits_batch_limit=count<=settings.qwen_batch_tokens)
        except (ValueError,AttributeError,TypeError) as error:item['tokenizer_error']=qwen.event_error_code(error)
        result[label]=item
    original=result['full'];focused=result['focused']
    savings={'input_bytes':original['input_bytes']-focused['input_bytes'],
             'bytes_percent':round(100*(1-focused['input_bytes']/max(1,original['input_bytes'])),1)}
    if 'input_tokens' in original and 'input_tokens' in focused:
        savings.update(input_tokens=original['input_tokens']-focused['input_tokens'],
                       tokens_percent=round(100*(1-focused['input_tokens']/max(1,original['input_tokens'])),1))
    result['savings']=savings
    return result


def inspect_context(settings):
    budget=Budget(settings,qwen._request)
    result={'model':settings.qwen_model,'context_tokens':budget.limit,'batch_token_limit':settings.qwen_batch_tokens,
            'parallelism':settings.qwen_parallelism,'request_timeout_seconds':settings.qwen_timeout}
    context=saved_context(settings)
    if context is None:return result|{'sample':'no_saved_analysis'}
    result['input_sections_bytes']={key:len(encoded(context.get(key)).encode()) for key in
                                   ('markets','team_statistics','archive_history','web_research','probability_assessment')}
    records=records_for(context,limit=record_limit(budget))
    for label,payload in (('raw_context',context),('single_evidence_request',evidence_payload(context,records))):
        messages=[{'role':'system','content':direct_system(context)},{'role':'user','content':encoded(payload)}]
        item={'input_bytes':sum(len(m['content'].encode()) for m in messages)}
        try:
            reply=qwen._request(replace(budget.token_settings,qwen_timeout=min(10,settings.qwen_timeout)),'post','/tokenize',
                json={'model':settings.qwen_model,'messages':messages,'add_generation_prompt':True})
            count=reply.get('count')
            if type(count) is not int or count<0:raise ValueError('Invalid tokenizer count')
            output=min(settings.qwen_max_tokens,512)
            required=count+output+256
            item.update(input_tokens=count,reserved_output_tokens=output,minimum_context_tokens=required,
                        fits_advertised_window=required<=budget.limit)
        except (ValueError,AttributeError,TypeError) as error:
            item['tokenizer_error']=qwen.event_error_code(error)
        result[label]=item
    return result


def check_load(settings):
    report={'thinking':settings.qwen_enable_thinking,'request_timeout_seconds':settings.qwen_timeout,
            'batch_token_limit':settings.qwen_batch_tokens,'parallelism':settings.qwen_parallelism,'probes':[]}
    def probe(stage,messages,max_tokens):
        payload={'model':settings.qwen_model,'temperature':0,'max_tokens':max_tokens,'stream':False,'messages':messages}
        if settings.qwen_enable_thinking is not None:payload['chat_template_kwargs']={'enable_thinking':settings.qwen_enable_thinking}
        item={'stage':stage,'input_bytes':sum(len(m['content'].encode()) for m in messages),'max_output_tokens':max_tokens}
        start=time.monotonic()
        try:
            reply=qwen._request(settings,'post','/chat/completions',json=payload)
            choice,message=qwen._chat_choice(reply)
            usage=reply.get('usage') or {}
            item.update(state='received',prompt_tokens=usage.get('prompt_tokens') if type(usage.get('prompt_tokens')) is int else None,
                        completion_tokens=usage.get('completion_tokens') if type(usage.get('completion_tokens')) is int else None,
                        response_characters=len(message.get('content')) if isinstance(message.get('content'),str) else 0,
                        reasoning_characters=len(message.get('reasoning_content')) if isinstance(message.get('reasoning_content'),str) else 0,
                        finish=choice.get('finish_reason') if choice.get('finish_reason') in ('stop','length','tool_calls','content_filter') else 'unknown')
        except (ValueError,AttributeError,TypeError) as error:
            item.update(state='failed',reason=qwen.event_error_code(error))
        item['elapsed_ms']=int((time.monotonic()-start)*1000);report['probes'].append(item)
        return item['state']=='received'
    if not probe('small',[{'role':'system','content':'Answer in one word.'},{'role':'user','content':'Say OK.'}],min(16,settings.qwen_max_tokens)):return report
    context=saved_context(settings)
    if context is None:report['sample']='no_saved_analysis';return report
    opts=replace(settings,qwen_batch_tokens=min(settings.qwen_batch_tokens,2048))
    budget=Budget(opts,qwen._request)
    source=records_for(context,limit=500)
    build=lambda values:evidence_payload(context,values)
    system=(FACT_EXTRACT+'\nFor this diagnostic select ONE existing fact ID.' if trusted_facts(context) else
            EXTRACT+'\nFor this diagnostic return ONE observation, text <=100 characters and ONE quote <=60 characters.')
    groups=budget.batches(source[:12],system,build,min(256,settings.qwen_max_tokens))
    if not groups:report['sample']='no_source_records';return report
    report['context_tokens']=budget.limit
    probe('match_sample',[{'role':'system','content':system},{'role':'user','content':encoded(build(groups[0]))}],min(256,settings.qwen_max_tokens))
    return report
