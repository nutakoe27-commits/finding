"""Request-isolated accounting of provider-reported Chat Completions usage."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from threading import Lock

_sinks=ContextVar('qwen_token_usage_sinks',default=())


def count(value):return value if type(value) is int and value>=0 else None


class Call:
    def __init__(self,max_output):
        self.lock=Lock();self.data={'max_output_tokens':count(max_output)}

    def finish(self,usage,finish):
        usage=usage if isinstance(usage,dict) else {}
        prompt=count(usage.get('prompt_tokens'));output=count(usage.get('completion_tokens'))
        total=prompt+output if prompt is not None and output is not None else None
        provided=count(usage.get('total_tokens'))
        # Disagreeing counts cannot be called complete factual accounting.
        if provided is not None and total is not None and provided!=total:prompt=output=total=None
        with self.lock:self.data.update(prompt_tokens=prompt,completion_tokens=output,total_tokens=total,
            finish_reason=finish if finish in ('stop','length','tool_calls','content_filter') else 'unknown')

    def snapshot(self):
        with self.lock:return dict(self.data)


class Capture:
    def __init__(self):self.calls=[];self.lock=Lock()

    def add(self,call):
        with self.lock:self.calls.append(call)

    def summary(self,expected_calls=0):
        with self.lock:rows=[c.snapshot() for c in self.calls]
        total_calls=max(len(rows),count(expected_calls) or 0)
        known=[r for r in rows if r.get('total_tokens') is not None]
        zero=total_calls==0
        return {'model_calls':total_calls,'reported_calls':len(known),'unreported_calls':total_calls-len(known),
            'complete':total_calls==len(known),
            'prompt_tokens':sum(r['prompt_tokens'] for r in known) if known or zero else None,
            'completion_tokens':sum(r['completion_tokens'] for r in known) if known or zero else None,
            'total_tokens':sum(r['total_tokens'] for r in known) if known or zero else None,
            'peak_prompt_tokens':max((r['prompt_tokens'] for r in known),default=0 if zero else None),
            'peak_total_tokens':max((r['total_tokens'] for r in known),default=0 if zero else None),
            'peak_context_tokens':max((r['prompt_tokens']+r['max_output_tokens'] for r in known if r['max_output_tokens'] is not None),default=0 if zero else None),
            'calls':rows}


@contextmanager
def capture_usage():
    capture=Capture();token=_sinks.set((*_sinks.get(),capture))
    try:yield capture
    finally:_sinks.reset(token)


def begin_call(max_output):
    call=Call(max_output)
    for sink in _sinks.get():sink.add(call)
    return call


def track_generation(function):
    @wraps(function)
    def wrapped(*args,**kwargs):
        with capture_usage() as capture:
            verdict,generation=function(*args,**kwargs)
        generation['token_usage']=capture.summary(generation.get('model_calls',0))
        return verdict,generation
    return wrapped
