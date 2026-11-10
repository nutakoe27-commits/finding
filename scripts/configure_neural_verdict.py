#!/usr/bin/env python3
"""Set only the Matchlab analysis mode and fixed stake ceiling; no service restart."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import re
import tempfile

ROOT=Path(__file__).resolve().parent.parent


def atomic(path,content):
    metadata=path.stat() if path.exists() else None
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as stream:
        candidate=Path(stream.name);os.fchmod(stream.fileno(),0o600)
        stream.write(content);stream.flush();os.fsync(stream.fileno())
    try:
        if metadata and os.geteuid()==0:os.chown(candidate,metadata.st_uid,metadata.st_gid)
        candidate.replace(path)
    finally:candidate.unlink(missing_ok=True)


def configure(path,mode='neural-verdict',stake=.5):
    path=Path(path)
    if path.is_symlink() or mode not in ('independent','neural-verdict') or not .01<=stake<=.5:raise ValueError('Некорректный режим, лимит или путь .env.')
    source=path.read_bytes() if path.exists() else b''
    content=source.decode('utf-8')
    for key,value in [('EVENT_ANALYSIS_MODE',mode),('VERDICT_MAX_STAKE_PCT',f'{stake:g}')]:
        pattern=re.compile(r'(?m)^[ \t]*(?:export[ \t]+)?'+key+r'[ \t]*=.*$')
        if len(pattern.findall(content))>1:raise ValueError('В .env найдены дубликаты '+key+'. Изменений нет.')
        binding=key+'='+value
        content=pattern.sub(binding,content) if pattern.search(content) else content+('' if not content or content.endswith('\n') else '\n')+binding+'\n'
    candidate=content.encode()
    if candidate!=source:
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists():
            backups=path.parent/'.local'/'verdict-config-backups';backups.mkdir(parents=True,mode=0o700,exist_ok=True)
            atomic(backups/('env-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')),source)
        atomic(path,candidate)
    return {'mode':mode,'max_stake_pct':stake,'changed':candidate!=source,'restart_performed':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path,default=ROOT/'.env')
    parser.add_argument('--mode',choices=('independent','neural-verdict'),default='neural-verdict')
    parser.add_argument('--max-stake-pct',type=float,default=.5)
    args=parser.parse_args()
    try:result=configure(args.env_file,args.mode,args.max_stake_pct)
    except (OSError,ValueError):parser.exit(1,'Настройка не изменена: проверьте путь, дубликаты ключей и лимит 0.01–0.5. Значения других настроек скрыты.\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
