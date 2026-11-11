#!/usr/bin/env python3
"""Create only a new, dedicated local Matchlab database/account via socket auth."""
import argparse
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
from urllib.parse import quote

ROOT=Path(__file__).resolve().parent.parent;sys.path.insert(0,str(ROOT))
from matchlab.config import Settings
from matchlab.database_migration import private_file,server_check
from matchlab.mysql_backend import MySQLTarget


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',default='matchlab_app');parser.add_argument('--user',default='matchlab_app')
    args=parser.parse_args()
    if any(not re.fullmatch(r'matchlab_[a-zA-Z0-9_]{1,24}',v) for v in (args.database,args.user)):parser.error('Use dedicated matchlab_* names, maximum 32 characters.')
    settings=Settings.from_env();pending=settings.data_dir/'mysql-target.env'
    if pending.exists():
        from dotenv import dotenv_values
        server_check(MySQLTarget(dotenv_values(pending)['MATCHLAB_MYSQL_TARGET_URL']))
        print('Закрытые настройки уже существуют и подключение проверено. Ничего не пересоздано.');return
    client=shutil.which('mysql')
    if not client:raise SystemExit('Не найден mysql client; работающая база не изменена.')
    def execute(sql):
        result=subprocess.run([client,'--protocol=socket','--user=root','--batch','--skip-column-names'],input=sql,text=True,capture_output=True)
        if result.returncode:raise ValueError('Локальная административная команда MySQL не выполнена. SQLite не изменена; секреты в вывод не включены.')
        return result.stdout.strip()
    try:
        existing=execute("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='"+args.database+"'; SELECT COUNT(*) FROM mysql.user WHERE user='"+args.user+"';")
        if any(int(x) for x in existing.splitlines()):raise ValueError('База или учётная запись уже существуют. Их изменение запрещено; выберите новые matchlab_* имена.')
        password=secrets.token_urlsafe(36)
        url='mysql://'+args.user+':'+quote(password,safe='')+'@127.0.0.1:3306/'+args.database
        settings.data_dir.mkdir(parents=True,exist_ok=True)
        private_file(pending).write_text("MATCHLAB_MYSQL_TARGET_URL='"+url+"'\n")
        execute("CREATE DATABASE `"+args.database+"` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;\n"
            "CREATE USER '"+args.user+"'@'127.0.0.1' IDENTIFIED BY '"+password+"';\n"
            "GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,ALTER,INDEX,REFERENCES ON `"+args.database+"`.* TO '"+args.user+"'@'127.0.0.1';")
        details=server_check(MySQLTarget(url))
        print(json.dumps({'state':'prepared','database':args.database,'user':args.user,'version':details['version'],'private_settings_file':str(pending),'active_database_changed':False},ensure_ascii=False,indent=2))
    except ValueError as error:raise SystemExit(str(error))


if __name__=='__main__':main()
