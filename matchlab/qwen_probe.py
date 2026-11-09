"""Two sequential, read-only model probes. No source content or credentials in output."""
from dataclasses import replace
import json
import sqlite3
import time
from urllib.parse import quote

from . import qwen
from .event_reasoning import Budget,EXTRACT,records_for,encoded,evidence_payload


def saved_context(settings):
    if not settings.database.is_file():return None
    with sqlite3.connect('file:'+quote(str(settings.database.resolve()))+'?mode=ro',uri=True) as db:
        row=db.execute('SELECT payload FROM event_analyses ORDER BY generated_at DESC LIMIT 1').fetchone()
    if not row:return None
    return {key:value for key,value in (json.loads(row[0]).get('context') or {}).items() if key!='ai_generation'}


def inspect_context(settings):
    budget=Budget(settings,qwen._request)
    result={'model':settings.qwen_model,'context_tokens':budget.limit,'batch_token_limit':settings.qwen_batch_tokens,
            'parallelism':settings.qwen_parallelism,'request_timeout_seconds':settings.qwen_timeout}
    context=saved_context(settings)
    if context is None:return result|{'sample':'no_saved_analysis'}
    records=records_for(context,limit=max(200,min(1800,budget.limit//4)))
    for label,payload in (('raw_context',context),('single_evidence_request',evidence_payload(context,records))):
        messages=[{'role':'system','content':EXTRACT},{'role':'user','content':encoded(payload)}]
        item={'input_bytes':sum(len(m['content'].encode()) for m in messages)}
        try:
            reply=qwen._request(replace(budget.token_settings,qwen_timeout=min(10,settings.qwen_timeout)),'post','/tokenize',
                json={'model':settings.qwen_model,'messages':messages,'add_generation_prompt':True})
            count=reply.get('count')
            if type(count) is not int or count<0:raise ValueError('Invalid tokenizer count')
            required=count+settings.qwen_max_tokens+256
            item.update(input_tokens=count,reserved_output_tokens=settings.qwen_max_tokens,minimum_context_tokens=required,
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
            usage=reply.get('usage') or {};choice=(reply.get('choices') or [{}])[0]
            message=choice.get('message') or {}
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
    system=EXTRACT+'\nFor this diagnostic return ONE observation, text <=100 characters and ONE quote <=60 characters.'
    groups=budget.batches(source[:12],system,build,min(256,settings.qwen_max_tokens))
    if not groups:report['sample']='no_source_records';return report
    report['context_tokens']=budget.limit
    probe('match_sample',[{'role':'system','content':system},{'role':'user','content':encoded(build(groups[0]))}],min(256,settings.qwen_max_tokens))
    return report
