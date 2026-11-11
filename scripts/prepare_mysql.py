#!/usr/bin/env python3
"""Create only a new, dedicated local Matchlab database/account via socket auth."""
import argparse
import getpass
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import quote

ROOT=Path(__file__).resolve().parent.parent;sys.path.insert(0,str(ROOT))
from matchlab.config import Settings
from matchlab.database_migration import private_file,server_check
from matchlab.mysql_backend import MySQLTarget


class MySQLAdmin:
    """Keep an optional administrative password out of argv, env and logs."""
    def __init__(self,client,user='root',password=None):
        self.directory=None;self.has_password=password is not None
        self.command=[client]
        if self.has_password:
            self.directory=tempfile.TemporaryDirectory(prefix='matchlab-mysql-admin-')
            path=private_file(Path(self.directory.name)/'client.cnf')
            escaped=password.replace('\\','\\\\').replace('"','\\"').replace('\n','\\n').replace('\r','\\r').replace('\t','\\t')
            path.write_text('[client]\npassword="'+escaped+'"\n',encoding='utf-8')
            self.command.append('--defaults-file='+str(path))
        self.command+=['--protocol=socket','--user='+user,'--batch','--skip-column-names']

    def execute(self,sql,stage):
        try:result=subprocess.run(self.command,input=sql,text=True,capture_output=True,timeout=30)
        except subprocess.TimeoutExpired:raise ValueError('MySQL: истекло время ожидания на этапе «'+stage+'». SQLite не изменена.') from None
        if result.returncode:
            match=re.search(r'\bERROR\s+(\d+)',result.stderr)
            code=match.group(1) if match else 'не определён'
            hint=''
            if code in ('1045','1698'):
                hint=(' Проверьте пароль или укажите действующего администратора через --admin-user.' if self.has_password else
                      ' Повторите с --ask-admin-password для скрытого ввода пароля администратора MySQL.')
            elif code in ('2002','2003'):hint=' Не удалось подключиться к локальному серверу MySQL.'
            elif code in ('1044','1142','1227'):hint=' У выбранного администратора недостаточно прав.'
            raise ValueError('MySQL: этап «'+stage+'», код '+code+'.'+hint+' SQLite не изменена.')
        return result.stdout.strip()

    def close(self):
        if self.directory:self.directory.cleanup()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',default='matchlab_app');parser.add_argument('--user',default='matchlab_app')
    parser.add_argument('--admin-user',default='root',help='Существующий локальный администратор MySQL; его учётная запись не изменяется.')
    parser.add_argument('--ask-admin-password',action='store_true',help='Скрыто запросить пароль администратора, без пароля в командной строке.')
    args=parser.parse_args()
    if any(not re.fullmatch(r'matchlab_[a-zA-Z0-9_]{1,24}',v) for v in (args.database,args.user)):parser.error('Use dedicated matchlab_* names, maximum 32 characters.')
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,32}',args.admin_user):parser.error('Некорректное имя администратора MySQL.')
    settings=Settings.from_env();pending=settings.data_dir/'mysql-target.env'
    if pending.exists():
        from dotenv import dotenv_values
        server_check(MySQLTarget(dotenv_values(pending)['MATCHLAB_MYSQL_TARGET_URL']))
        print('Закрытые настройки уже существуют и подключение проверено. Ничего не пересоздано.');return
    client=shutil.which('mysql')
    if not client:raise SystemExit('Не найден mysql client; работающая база не изменена.')
    admin_password=getpass.getpass('Пароль администратора MySQL '+args.admin_user+' (ввод скрыт): ') if args.ask_admin_password else None
    admin=MySQLAdmin(client,args.admin_user,admin_password)
    try:
        existing=admin.execute("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='"+args.database+"'; SELECT COUNT(*) FROM mysql.user WHERE user='"+args.user+"';",'проверка существующих реквизитов')
        if any(int(x) for x in existing.splitlines()):raise ValueError('База или учётная запись уже существуют. Их изменение запрещено; выберите новые matchlab_* имена.')
        application_password=secrets.token_urlsafe(36)
        url='mysql://'+args.user+':'+quote(application_password,safe='')+'@127.0.0.1:3306/'+args.database
        settings.data_dir.mkdir(parents=True,exist_ok=True)
        private_file(pending).write_text("MATCHLAB_MYSQL_TARGET_URL='"+url+"'\n")
        admin.execute("CREATE DATABASE `"+args.database+"` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;\n"
            "CREATE USER '"+args.user+"'@'127.0.0.1' IDENTIFIED BY '"+application_password+"';\n"
            "GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,ALTER,INDEX,REFERENCES ON `"+args.database+"`.* TO '"+args.user+"'@'127.0.0.1';",'создание отдельной базы и пользователя Matchlab')
        details=server_check(MySQLTarget(url))
        print(json.dumps({'state':'prepared','database':args.database,'user':args.user,'version':details['version'],'private_settings_file':str(pending),'active_database_changed':False},ensure_ascii=False,indent=2))
    except ValueError as error:raise SystemExit(str(error))
    finally:admin.close()


if __name__=='__main__':main()
