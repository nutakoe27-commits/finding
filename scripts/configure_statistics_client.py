#!/usr/bin/env python3
"""Copy only the existing collector key over authorized SSH into a private local file."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

REMOTE="cd /root/matchlab && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -c 'import json; from matchlab.config import Settings; print(json.dumps({\"collector_key\":Settings.from_env().bookmaker_ingest_token}))'"


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-host',default='root@89.111.143.45')
    parser.add_argument('--api-url',default='http://127.0.0.1:18082')
    parser.add_argument('--output',type=Path,default=Path('.local/statistics-client.json'))
    args=parser.parse_args()
    if not re.fullmatch(r'(?:[A-Za-z_][A-Za-z0-9_.-]*@)?[A-Za-z0-9][A-Za-z0-9.-]*',args.ssh_host): parser.error('Некорректный SSH-хост.')
    url=urlsplit(args.api_url)
    if url.scheme!='http' or url.hostname not in ('127.0.0.1','localhost','::1') or url.username or url.password or url.path not in ('','/') or url.query or url.fragment: parser.error('Нужен локальный HTTP API через SSH-туннель.')
    os.umask(0o077)
    if args.output.is_symlink(): parser.error('Файл настройки не должен быть ссылкой.')
    if args.output.exists():
        try: existing=json.loads(args.output.read_text())
        except (OSError,ValueError): parser.error('Выходной файл уже занят другой конфигурацией.')
        if existing.get('format')!='matchlab.statistics-client.v1': parser.error('Выходной файл уже занят другой конфигурацией.')
    result=subprocess.run(['ssh',args.ssh_host,REMOTE],stdout=subprocess.PIPE,check=False)
    if result.returncode: parser.exit(1,'Не удалось получить ключ по SSH. Данные не записаны.\n')
    try:
        data=json.loads(result.stdout)
        key=data['collector_key']
        if not isinstance(key,str) or not 32<=len(key)<=500 or any(ord(char)<32 for char in key): raise ValueError()
    except (ValueError,KeyError,TypeError): parser.exit(1,'На VPS не найден действующий ключ приёмника. Ответ SSH не выводится.\n')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    flags=os.O_WRONLY|os.O_CREAT|os.O_TRUNC|getattr(os,'O_NOFOLLOW',0)
    with os.fdopen(os.open(args.output,flags,0o600),'w') as output:
        os.fchmod(output.fileno(),0o600)
        json.dump({'format':'matchlab.statistics-client.v1','api_url':args.api_url,'collector_key':key},output)
    print('Настройка сохранена:',args.output,'— права 600. Ключ не выводится.')


if __name__=='__main__': main()
