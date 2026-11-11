#!/usr/bin/env python3
"""Rehearse first; switch only with a stopped application and matching hashes."""
import argparse
from datetime import datetime,timezone
import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from matchlab.config import Settings
from matchlab.database_migration import migrate,inventory,private_file,export_sqlite
from matchlab.mysql_backend import MySQLTarget,readonly


def stopped(unit):
    if unit!='matchlab.service':raise ValueError('Допускается проверка только matchlab.service.')
    value=subprocess.run(['systemctl','is-active',unit],capture_output=True,text=True).stdout.strip()
    if value not in ('inactive','failed'):raise ValueError('Сначала остановите matchlab.service. Миграция не отключает службы самостоятельно.')
    push=subprocess.run(['systemctl','is-active','matchlab-push.service'],capture_output=True,text=True).stdout.strip()
    if push not in ('inactive','failed','unknown',''):raise ValueError('Остановите также matchlab-push.service: она использует ту же базу.')


def env_update(path,value):
    from dotenv import set_key
    backup=path.with_name('.env.before-database-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    if path.exists():shutil.copyfile(path,private_file(backup))
    else:private_file(path)
    set_key(str(path),'MATCHLAB_DATABASE_URL',value,quote_mode='always');path.chmod(0o600)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true',help='Скопировать и сохранить данные после проверки; по умолчанию только репетиция.')
    parser.add_argument('--activate',action='store_true',help='После проверенного --apply обновить .env. Служба должна быть остановлена.')
    parser.add_argument('--activate-verified',action='store_true',help='Завершить переключение ранее проверенного импорта без повторного копирования.')
    parser.add_argument('--rollback',action='store_true',help='Выгрузить актуальную MySQL в новую SQLite и переключить .env; старую SQLite сохранить.')
    parser.add_argument('--service',default='matchlab.service')
    args=parser.parse_args();settings=Settings.from_env();stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder=settings.data_dir/'database-migrations';folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    try:
        if sum((args.apply,args.rollback,args.activate_verified))>1:raise ValueError('Выберите один режим: --apply, --rollback или --activate-verified.')
        if args.activate and not args.apply:raise ValueError('--activate требует --apply.')
        if args.apply or args.rollback or args.activate_verified:stopped(args.service)
        if args.rollback:
            if not isinstance(settings.database,MySQLTarget):raise ValueError('Активная база не MySQL.')
            fresh=export_sqlite(settings.database,folder/('rollback-'+stamp+'.sqlite3'));stopped(args.service)
            original=settings.data_dir/'matchlab.sqlite3'
            if original.exists():original.rename(folder/('original-sqlite-'+stamp+'.sqlite3'))
            for suffix in ('-wal','-shm','-journal'):
                sidecar=Path(str(original)+suffix)
                if sidecar.exists():sidecar.rename(folder/('original-sqlite-'+stamp+'.sqlite3'+suffix))
            fresh.replace(original);env_update(ROOT/'.env','')
            print('Актуальные данные MySQL проверены и сохранены в SQLite. .env переключён; служба не запущена.');return
        if isinstance(settings.database,MySQLTarget):raise ValueError('MySQL уже активна; повторный импорт запрещён.')
        from dotenv import dotenv_values
        pending=dotenv_values(settings.data_dir/'mysql-target.env')
        url=os.environ.get('MATCHLAB_MYSQL_TARGET_URL') or pending.get('MATCHLAB_MYSQL_TARGET_URL') or getpass.getpass('URL отдельной пустой MySQL базы (ввод скрыт): ')
        target=MySQLTarget(url);target.options()
        def unchanged(expected,schema):
            stopped(args.service)
            with readonly(settings.database) as source:
                source.execute('BEGIN')
                if inventory(source,schema)!=expected:raise ValueError('SQLite изменилась после снимка. Переключение отменено, источник сохранён.')
        if args.activate_verified:
            from contextlib import closing
            from matchlab.mysql_backend import Connection
            with closing(Connection(target,readonly=True)) as db:
                cursor=db.raw.cursor();cursor.execute('SELECT payload FROM `_matchlab_migration` WHERE id=1');report=json.loads(cursor.fetchone()[0])
                if report.get('format')!='matchlab.mysql-migration.v1' or report.get('state')!='verified' or inventory(db,report['schema'])!=report['tables']:raise ValueError('Сохранённый импорт не подтверждён или изменился.')
            unchanged(report['tables'],report['schema']);env_update(ROOT/'.env',url)
            print('Проверенный импорт активирован. Службы не запущены.');return
        report=migrate(settings.database,target,folder/('before-mysql-'+stamp+'.sqlite3'),apply=args.apply,frozen_check=unchanged if args.apply else None)
        path=private_file(folder/('report-'+stamp+'.json'));path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        if args.activate:unchanged(report['tables'],report['schema']);env_update(ROOT/'.env',url)
        print(json.dumps({'state':report['state'],'tables':len(report['tables']),'rows':sum(x['rows'] for x in report['tables'].values()),'report':str(path),'activated':args.activate,'service_started':False},ensure_ascii=False,indent=2))
    except Exception as error:
        # Never echo a DSN/password or a driver error containing user records.
        print('Перенос/переключение не завершено. Исходные данные не удалены. '+(str(error) if isinstance(error,ValueError) else type(error).__name__),file=sys.stderr)
        raise SystemExit(1)


if __name__=='__main__':main()
