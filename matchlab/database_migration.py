"""Verified, non-destructive application-data migration and portable backups."""
from contextlib import closing
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import shutil

from .mysql_backend import Connection,MySQLTarget,inspect_sqlite,initialize,quote


FORMAT='matchlab.mysql-migration.v1'


def private_file(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    return path


def sqlite_snapshot(source,destination):
    from .mysql_backend import readonly
    destination=private_file(destination)
    try:
        with readonly(source) as src,closing(sqlite3.connect(destination)) as dst:src.backup(dst)
        with closing(sqlite3.connect(destination)) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('SQLite integrity check failed')
    except BaseException:destination.unlink(missing_ok=True);raise
    return destination


def digest(db,table):
    columns=','.join(quote(c['name']) for c in table['columns'])
    cursor=db.execute('SELECT '+columns+' FROM '+quote(table['name']))
    hashes=[]
    while rows:=cursor.fetchmany(256):
        for row in rows:
            h=hashlib.sha256()
            for value in row:
                if value is None:kind,data=b'n',b''
                elif isinstance(value,int):kind,data=b'i',str(value).encode()
                elif isinstance(value,float):kind,data=b'f',struct.pack('!d',value)
                elif isinstance(value,str):kind,data=b's',value.encode('utf-8')
                elif isinstance(value,bytes):kind,data=b'b',value
                else:raise ValueError('Unsupported value type in '+table['name'])
                h.update(kind+struct.pack('!Q',len(data))+data)
            hashes.append(h.digest())
    return {'rows':len(hashes),'sha256':hashlib.sha256(b''.join(sorted(hashes))).hexdigest()}


def inventory(db,schema):return {t['name']:digest(db,t) for t in schema}


def server_check(target):
    with closing(Connection(target)) as db:
        version=db.execute('SELECT VERSION()').fetchone()[0]
        import re
        number=tuple(map(int,re.search(r'(\d+)\.(\d+)\.(\d+)',version).groups()))
        minimum=(10,6,0) if 'mariadb' in version.lower() else (8,0,16)
        if number<minimum:raise ValueError('Нужен MySQL 8.0.16+ либо MariaDB 10.6+. Обнаружено: '+version)
        return {'version':version,'max_allowed_packet':db.execute('SELECT @@max_allowed_packet').fetchone()[0]}


def owned_target(target,schema):
    """Never import into an unrelated database or replace existing business rows."""
    with closing(Connection(target)) as db:
        names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if names and '_matchlab_migration' not in names:raise ValueError('Целевая база не пуста и не подготовлена мигратором Matchlab. Ничего не изменено.')
        if '_matchlab_migration' in names:
            marker=db.raw.cursor();marker.execute('SELECT payload FROM `_matchlab_migration` WHERE id=1');row=marker.fetchone()
            if not row or json.loads(row[0]).get('format')!=FORMAT:raise ValueError('Неизвестный владелец целевой базы.')
        if names-{'_matchlab_migration'}-{t['name'] for t in schema}:raise ValueError('В целевой базе есть посторонние таблицы.')
        for name in names-{'_matchlab_migration'}:
            if db.execute('SELECT COUNT(*) FROM '+quote(name)).fetchone()[0]:raise ValueError('В целевой базе уже есть данные: '+name+'. Перезапись запрещена.')
        if not names:
            cursor=db.raw.cursor();cursor.execute('CREATE TABLE `_matchlab_migration` (id INT PRIMARY KEY,payload LONGTEXT NOT NULL) ENGINE=InnoDB CHARACTER SET utf8mb4 COLLATE utf8mb4_bin')
            cursor.execute('INSERT INTO `_matchlab_migration` VALUES (1,%s)',(json.dumps({'format':FORMAT,'state':'prepared','schema':schema}),))
    initialize(target,schema)


def copy_rows(source,destination,schema):
    for table in schema:
        names=[c['name'] for c in table['columns']]
        sql='INSERT INTO '+quote(table['name'])+' ('+','.join(map(quote,names))+') VALUES ('+','.join('?' for _ in names)+')'
        rows=source.execute('SELECT '+','.join(map(quote,names))+' FROM '+quote(table['name']))
        while batch:=rows.fetchmany(128):destination.executemany(sql,[tuple(row) for row in batch])


def migrate(source,target,backup,*,apply=False,frozen_check=None):
    """A rehearsal rolls back every copied row after checking all hashes."""
    details=server_check(target)
    size=sum(p.stat().st_size for p in (Path(source),Path(str(source)+'-wal')) if p.exists())
    folder=Path(backup).parent;folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    if shutil.disk_usage(folder).free<size*4+256*1024**2:raise ValueError('Недостаточно свободного места для снимков и переноса. Исходная база сохранена.')
    snapshot=sqlite_snapshot(source,backup)
    with closing(sqlite3.connect(snapshot)) as src:
        src.row_factory=sqlite3.Row
        unsupported=src.execute("SELECT name FROM sqlite_master WHERE type IN ('trigger','view')").fetchall()
        if unsupported:raise ValueError('Обнаружены представления/триггеры SQLite; требуется отдельная проверка, перенос остановлен.')
        schema=inspect_sqlite(src)
        expected=inventory(src,schema)
        owned_target(target,schema)
        with closing(Connection(target)) as dst:
            dst.begin(True)
            try:
                if any(dst.execute('SELECT COUNT(*) FROM '+quote(t['name'])).fetchone()[0] for t in schema):raise ValueError('Целевая база изменилась; перенос отменён.')
                copy_rows(src,dst,schema)
                actual=inventory(dst,schema)
                if actual!=expected:raise ValueError('Число записей или контрольные суммы не совпали. Переключение запрещено.')
                if frozen_check:frozen_check(expected,schema)
                report={'format':FORMAT,'state':'verified' if apply else 'rehearsal_verified',
                    'created_at':datetime.now(timezone.utc).isoformat(),'server':details,'tables':actual,
                    'backup':str(snapshot),'schema':schema}
                if apply:
                    dst.raw.cursor().execute('UPDATE `_matchlab_migration` SET payload=%s WHERE id=1',(json.dumps(report,ensure_ascii=False),))
                    dst.commit()
                else:dst.rollback()
                return report
            except BaseException:dst.rollback();raise


def export_sqlite(target,destination):
    """Consistent full backup, including new MySQL records, usable for rollback."""
    destination=private_file(destination)
    try:
        with closing(Connection(target,readonly=True)) as src,closing(sqlite3.connect(destination)) as dst:
            cursor=src.raw.cursor();cursor.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY');src.in_transaction=True
            cursor.execute('SELECT payload FROM `_matchlab_migration` WHERE id=1');marker=cursor.fetchone()
            marker=json.loads(marker[0]) if marker else {}
            if marker.get('format')!=FORMAT or marker.get('state')!='verified':
                raise ValueError('Экспорт неподтверждённой MySQL запрещён: нельзя заменить рабочую SQLite пустой репетицией.')
            schema=marker['schema']
            cursor.execute("SELECT table_name,column_name,extra FROM information_schema.columns WHERE table_schema=DATABASE()")
            actual={}
            for table,name,extra in cursor.fetchall():
                if table!='_matchlab_migration' and 'GENERATED' not in extra:actual.setdefault(table,set()).add(name)
            if actual!={t['name']:{c['name'] for c in t['columns']} for t in schema}:
                raise ValueError('Схема MySQL изменилась: нельзя пропускать новые таблицы или поля в резервной копии. Требуется обновить описание миграции.')
            for table in schema:dst.execute(table['sql'])
            copy_rows(src,dst,schema);dst.commit();dst.row_factory=sqlite3.Row
            if inventory(src,schema)!=inventory(dst,schema):raise ValueError('Backup verification failed')
            for table in schema:
                for index in table['indexes']:
                    if index['sql']:dst.execute(index['sql'])
            dst.commit()
            if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Backup integrity check failed')
        return destination
    except BaseException:destination.unlink(missing_ok=True);raise
