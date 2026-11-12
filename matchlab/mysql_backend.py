"""MySQL 8 adapter for the repository's parameterised SQL and transaction contract.

The write mutex deliberately preserves SQLite's serialised financial/queue
transactions during this first migration. It is shared by processes, not threads.
No user value is interpolated into SQL. Binary keys preserve SQLite equality,
including case and trailing spaces; text payloads and blobs remain unmodified.
"""
from dataclasses import dataclass,field
from functools import lru_cache
import hashlib
import json
import re
import sqlite3
from decimal import Decimal
from contextlib import contextmanager
from urllib.parse import urlsplit,unquote


@dataclass(frozen=True)
class MySQLTarget:
    url: str = field(repr=False)

    def __str__(self):return 'MySQL (private connection settings)'

    def options(self):
        p=urlsplit(self.url)
        if p.scheme not in ('mysql','mysql+pymysql') or not p.hostname or not p.username or p.query or p.fragment:
            raise ValueError('MATCHLAB_DATABASE_URL: нужен mysql://user:password@host:port/database без query.')
        name=unquote(p.path.lstrip('/'))
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',name):raise ValueError('Некорректное имя отдельной базы Matchlab.')
        return dict(host=p.hostname,port=p.port or 3306,user=unquote(p.username),password=unquote(p.password or ''),database=name)


class Row:
    def __init__(self,names,values):self.names=names;self.values=tuple(values)
    def __getitem__(self,key):return self.values[key if isinstance(key,(int,slice)) else self.names.index(key)]
    def __iter__(self):return iter(self.values)
    def __len__(self):return len(self.values)
    def keys(self):return self.names


class Cursor:
    def __init__(self,cursor):
        self.cursor=cursor;self.rowcount=cursor.rowcount;self.lastrowid=getattr(cursor,'lastrowid',None)
        self.names=[x[0] for x in cursor.description] if cursor.description else []
    def row(self,value):
        if value is None:return None
        # VARBINARY keys are textual identifiers; LONGBLOB payloads stay bytes.
        result=[]
        for i,v in enumerate(value):
            field=self.cursor._result.fields[i]
            if isinstance(v,bytes) and (self.cursor.description[i][1] in (15,253,254) or not field.org_table):
                try:v=v.decode('utf-8')
                except UnicodeDecodeError:pass
            elif isinstance(v,Decimal):v=int(v) if v==v.to_integral_value() else float(v)
            result.append(v)
        return Row(self.names,result)
    def fetchone(self):return self.row(self.cursor.fetchone())
    def fetchall(self):return [self.row(x) for x in self.cursor.fetchall()]
    def fetchmany(self,size=1000):return [self.row(x) for x in self.cursor.fetchmany(size)]
    def __iter__(self):
        while (row:=self.fetchone()) is not None:yield row


def _native(expression,source):
    from sqlglot import parse_one,exp
    tree=parse_one(expression,read='mysql')
    for function in tree.find_all(exp.Anonymous):
        if function.name.upper()=='TIMESTAMPADD':function.set('expressions',[exp.Var(this=function.expressions[0].name),*function.expressions[1:]])
    return tree.transform(lambda n:source.copy() if isinstance(n,exp.Column) and n.name=='__ml_value' else n)


def _transform(node):
    from sqlglot import exp,parse_one
    if isinstance(node,exp.Subquery) and isinstance(node.parent,(exp.From,exp.Join)) and not node.alias:
        node.set('alias',exp.TableAlias(this=exp.to_identifier('_ml_source_'+hashlib.sha256(node.this.sql().encode()).hexdigest()[:12])))
        return node
    if isinstance(node,exp.JSONType):
        value=exp.Anonymous(this='JSON_EXTRACT',expressions=[node.this.copy(),node.expression.copy()]) if node.expression is not None else node.this.copy()
        return _native("CASE JSON_TYPE(__ml_value) WHEN 'STRING' THEN 'text' WHEN 'DOUBLE' THEN 'real' WHEN 'INTEGER' THEN 'integer' WHEN 'BOOLEAN' THEN JSON_UNQUOTE(__ml_value) ELSE LOWER(JSON_TYPE(__ml_value)) END",value)
    if isinstance(node,exp.JSONExtract):
        paths=[node.expression]+list(node.expressions)
        if len(paths)>1:
            return exp.Anonymous(this='JSON_ARRAY',expressions=[exp.Anonymous(this='JSON_EXTRACT',expressions=[node.this.copy(),p.copy()]) for p in paths])
        value=exp.Anonymous(this='JSON_EXTRACT',expressions=[node.this.copy(),node.expression.copy()])
        return _native("CASE WHEN JSON_TYPE(__ml_value)='NULL' THEN NULL WHEN JSON_TYPE(__ml_value)='BOOLEAN' THEN IF(JSON_UNQUOTE(__ml_value)='true',1,0) ELSE JSON_UNQUOTE(__ml_value) END",value)
    if isinstance(node,exp.Date):
        modifier=node.args.get('zone')
        if modifier is not None and modifier.name!='+3 hours':raise ValueError('Unsupported SQLite date modifier')
        return _native("""DATE(TIMESTAMPADD(MINUTE,
            180-CASE WHEN CAST(RIGHT(__ml_value,6) AS CHAR CHARACTER SET utf8mb4) REGEXP '^[+-][0-9]{2}:[0-9]{2}$'
                THEN (CASE WHEN LEFT(RIGHT(__ml_value,6),1)='-' THEN -1 ELSE 1 END)*(CAST(SUBSTRING(RIGHT(__ml_value,6),2,2) AS SIGNED)*60+CAST(RIGHT(__ml_value,2) AS SIGNED)) ELSE 0 END,
            COALESCE(STR_TO_DATE(LEFT(REPLACE(__ml_value,'T',' '),19),'%Y-%m-%d %H:%i:%s'),STR_TO_DATE(LEFT(__ml_value,10),'%Y-%m-%d'))))""".replace('180-',('180-' if modifier is not None else '0-')),node.this)
    if isinstance(node,exp.Anonymous) and node.name.upper()=='CASEFOLD':
        return exp.Lower(this=node.expressions[0].copy())
    if isinstance(node,exp.Anonymous) and node.name.upper()=='JULIANDAY':
        return _native("""(ROUND(TIMESTAMPDIFF(MICROSECOND,'1970-01-01 00:00:00',
            COALESCE(STR_TO_DATE(LEFT(REPLACE(__ml_value,'T',' '),26),'%Y-%m-%d %H:%i:%s.%f'),
                     STR_TO_DATE(LEFT(REPLACE(__ml_value,'T',' '),19),'%Y-%m-%d %H:%i:%s'),
                     STR_TO_DATE(LEFT(__ml_value,10),'%Y-%m-%d')))/1000.0)/86400000.0+2440587.5
            -CASE WHEN CAST(RIGHT(__ml_value,6) AS CHAR CHARACTER SET utf8mb4) REGEXP '^[+-][0-9]{2}:[0-9]{2}$'
              THEN (CASE WHEN LEFT(RIGHT(__ml_value,6),1)='-' THEN -1 ELSE 1 END)
                *(CAST(SUBSTRING(RIGHT(__ml_value,6),2,2) AS SIGNED)*60+CAST(RIGHT(__ml_value,2) AS SIGNED))/1440.0 ELSE 0 END)""",node.expressions[0])
    if isinstance(node,exp.Table) and node.name=='sqlite_master':
        return parse_one("SELECT table_name AS name,'table' AS type FROM information_schema.tables WHERE table_schema=DATABASE() AND table_type='BASE TABLE'",read='mysql').subquery(alias='sqlite_master')
    return node


@lru_cache(maxsize=2048)
def compile_sql(sql):
    from sqlglot import parse_one,exp,Tokenizer,TokenType
    from sqlglot.dialects.mysql import MySQL
    class Generator(MySQL.Generator):
        def placeholder_sql(self,expression):return '__ml_bind_'+str(expression.this)+'__'
    tokens=[t for t in Tokenizer().tokenize(sql) if t.token_type==TokenType.PLACEHOLDER and t.text=='?']
    for i,t in reversed(list(enumerate(tokens))):sql=sql[:t.start]+':p'+str(i)+sql[t.end+1:]
    tree=parse_one(sql,read='sqlite')
    tree=tree.transform(_transform)
    names=tuple(sorted({p.this for p in tree.find_all(exp.Placeholder)}))
    conflict=None;alternative=None
    if isinstance(tree,exp.Insert):
        conflict=tree.args.get('conflict');tree.set('conflict',None)
        alternative=tree.args.get('alternative');tree.set('alternative',None)
        if isinstance(tree.this,exp.Table):
            schema=next((t for t in template_schema() if t['name']==tree.this.name),None)
            if schema:tree.set('this',exp.Schema(this=tree.this,expressions=[exp.to_identifier(c['name']) for c in schema['columns']]))
    result=Generator(identify=True,dialect=MySQL()).generate(tree)
    if alternative=='REPLACE':result=re.sub(r'^INSERT\b','REPLACE',result)
    elif alternative=='IGNORE':
        # Ignore duplicate keys only; never suppress truncation or invalid data.
        table=tree.this.this if isinstance(tree.this,exp.Schema) else tree.this
        schema=next(t for t in template_schema() if t['name']==table.name)
        key=next(c['name'] for c in schema['columns'] if c['pk'])
        result+=' ON DUPLICATE KEY UPDATE '+quote(key)+'='+quote(table.name)+'.'+quote(key)
    if conflict:
        assignments=[]
        for item in conflict.expressions:
            def excluded(n):
                if isinstance(n,exp.Column) and n.table=='excluded':return exp.Anonymous(this='VALUES',expressions=[exp.column(n.name)])
                return n
            assignments.append(Generator(identify=True,dialect=MySQL()).generate(item.transform(excluded)))
        result+=' ON DUPLICATE KEY UPDATE '+', '.join(assignments)
    result=result.replace('%','%%')
    for name in names:result=result.replace('__ml_bind_'+name+'__','%('+name+')s')
    return result,names


def quote(name):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',name):raise ValueError('Unsupported database identifier')
    return '`'+name+'`'


class Connection:
    dialect='mysql'
    def __init__(self,target,readonly=False):
        import pymysql
        from pymysql.constants import CLIENT
        self.raw=pymysql.connect(**target.options(),charset='utf8mb4',autocommit=True,connect_timeout=10,
            read_timeout=120,write_timeout=120,client_flag=CLIENT.FOUND_ROWS)
        self.raw.cursor().execute("SET SESSION time_zone='+00:00'")
        self.raw.cursor().execute("SET SESSION sql_mode='STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_AUTO_VALUE_ON_ZERO'")
        self.raw.cursor().execute('SET SESSION div_precision_increment=12')
        self.readonly=readonly;self.in_transaction=False;self.locked=False
        self.lock_name='matchlab-write-'+hashlib.sha256(target.options()['database'].encode()).hexdigest()[:40]
        if readonly:self.raw.cursor().execute('SET SESSION TRANSACTION READ ONLY')
    def begin(self,write=False):
        if write and self.readonly:raise sqlite3.OperationalError('Read-only connection')
        if write and not self.locked:
            c=self.raw.cursor();c.execute('SELECT GET_LOCK(%s,30)',(self.lock_name,))
            if c.fetchone()[0]!=1:raise sqlite3.OperationalError('Database write lock timeout')
            self.locked=True
        if not self.in_transaction:self.raw.begin();self.in_transaction=True
    def execute(self,sql,params=()):
        import pymysql
        if sql.strip().upper() in ('BEGIN','BEGIN IMMEDIATE'):
            self.begin(sql.strip().upper()=='BEGIN IMMEDIATE');return Cursor(self.raw.cursor())
        if sql.strip().upper() in ('COMMIT','ROLLBACK'):
            getattr(self,sql.strip().lower())();return Cursor(self.raw.cursor())
        if re.match(r'^\s*(INSERT|UPDATE|DELETE|REPLACE)\b',sql,re.I):self.begin(True)
        translated,names=compile_sql(sql)
        if isinstance(params,dict):
            if not set(names)<=params.keys():raise ValueError('Missing named SQL parameter')
            bound={name:params[name] for name in names}
        else:
            if len(params)!=len(names):raise ValueError('Wrong SQL parameter count')
            bound={'p'+str(i):v for i,v in enumerate(params)}
        try:
            c=self.raw.cursor();c.execute(translated,bound);return Cursor(c)
        except pymysql.IntegrityError as e:raise sqlite3.IntegrityError('MySQL integrity constraint rejected the operation') from e
        except pymysql.MySQLError as e:raise sqlite3.OperationalError('MySQL operation failed (code '+str(e.args[0])+')') from e
    def executemany(self,sql,rows):
        import pymysql
        rows=list(rows)
        if not rows:return Cursor(self.raw.cursor())
        self.begin(True);translated,names=compile_sql(sql);bound=[]
        for params in rows:
            if isinstance(params,dict):
                if not set(names)<=params.keys():raise ValueError('Missing named SQL parameter')
                bound.append({name:params[name] for name in names})
            else:
                if len(params)!=len(names):raise ValueError('Wrong SQL parameter count')
                bound.append({'p'+str(i):v for i,v in enumerate(params)})
        try:
            cursor=self.raw.cursor();cursor.executemany(translated,bound);return Cursor(cursor)
        except pymysql.IntegrityError as e:raise sqlite3.IntegrityError('MySQL integrity constraint rejected the batch') from e
        except pymysql.MySQLError as e:raise sqlite3.OperationalError('MySQL batch failed (code '+str(e.args[0])+')') from e
    def create_function(self,name,*args,**kwargs):
        if name!='casefold':raise ValueError('Python SQL function must be evaluated outside MySQL: '+name)
    def _finish(self,rollback):
        try:self.raw.rollback() if rollback else self.raw.commit()
        finally:
            self.in_transaction=False
            if self.locked:
                self.raw.cursor().execute('SELECT RELEASE_LOCK(%s)',(self.lock_name,));self.locked=False
    def commit(self):self._finish(False)
    def rollback(self):self._finish(True)
    def close(self):self.raw.close()


@lru_cache(maxsize=1)
def template_schema():
    import tempfile
    from pathlib import Path
    from .storage import Repository
    with tempfile.TemporaryDirectory(prefix='matchlab-schema-') as folder:
        repo=Repository(Path(folder)/'schema.sqlite3');repo.initialize()
        with repo.connection() as db:return inspect_sqlite(db)


def inspect_sqlite(db):
    result=[]
    for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        name=row['name'];quote(name)
        columns=[dict(x) for x in db.execute('PRAGMA table_info('+quote(name)+')')]
        if any(r['hidden'] for r in db.execute('PRAGMA table_xinfo('+quote(name)+')')):
            raise ValueError('Generated SQLite source columns require explicit migration: '+name)
        indexes=[]
        for index in db.execute('PRAGMA index_list('+quote(name)+')'):
            parts=[dict(x) for x in db.execute('PRAGMA index_info('+quote(index['name'])+')')]
            sql=db.execute('SELECT sql FROM sqlite_master WHERE name=?',(index['name'],)).fetchone()[0]
            indexes.append(dict(name=index['name'],unique=bool(index['unique']),parts=parts,sql=sql,origin=index['origin']))
        result.append(dict(name=name,sql=row['sql'],columns=columns,indexes=sorted(indexes,key=lambda x:x['name'])))
    return result


def schema_statements(table):
    from sqlglot import parse_one,exp
    name=table['name'];indexed={c['name'] for c in table['columns'] if c['pk']}
    for index in table['indexes']:
        indexed.update(p['name'] for p in index['parts'] if p['name'])
    definitions=[];extra=[];indexes=[]
    tree=parse_one(table['sql'],read='sqlite')
    if any(any(word in type(n).__name__ for word in ('ForeignKey','Reference','Collate')) for n in tree.walk()):
        raise ValueError('Nonstandard SQLite constraints require explicit migration: '+name)
    checked={c.name for check in tree.find_all(exp.CheckColumnConstraint) for c in check.find_all(exp.Column)}
    for c in table['columns']:
        kind=c['type'].upper()
        if kind=='TEXT':kind='VARBINARY(1024)' if c['name'] in indexed or c['dflt_value'] is not None else 'LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin'
        elif kind in ('INTEGER','INT'):kind='BIGINT'
        elif kind=='BLOB':kind='LONGBLOB'
        elif kind in ('REAL','FLOAT','DOUBLE'):kind='DOUBLE'
        else:raise ValueError('Unsupported source column type: '+kind)
        definition=quote(c['name'])+' '+kind
        if c['notnull'] or c['pk']:definition+=' NOT NULL'
        if c['pk'] and c['name'] not in checked and c['type'].upper()=='INTEGER' and sum(bool(x['pk']) for x in table['columns'])==1:definition+=' AUTO_INCREMENT'
        if c['dflt_value'] is not None:definition+=' DEFAULT '+c['dflt_value']
        definitions.append(definition)
    primary=[quote(c['name']) for c in sorted(table['columns'],key=lambda x:x['pk']) if c['pk']]
    if primary:definitions.append('PRIMARY KEY ('+','.join(primary)+')')
    # Preserve source CHECK constraints in native InnoDB as well as app validation.
    for check in tree.find_all(exp.CheckColumnConstraint):definitions.append(check.sql(dialect='mysql',identify=True))
    for index in table['indexes']:
        if index['origin']=='pk':continue
        parts=[];predicate=None
        if index['sql']:
            parsed=parse_one(index['sql'],read='sqlite').this
            params=parsed.args['params'];predicate=params.args.get('where')
            for i,item in enumerate(params.args['columns']):
                expression=item.this
                if isinstance(expression,exp.Column):parts.append(quote(expression.name))
                else:
                    generated='_ml_expr_'+hashlib.sha256((index['name']+str(i)).encode()).hexdigest()[:16]
                    native=expression.transform(_transform).sql(dialect='mysql',identify=True)
                    extra.append(quote(generated)+' VARBINARY(1024) GENERATED ALWAYS AS ('+native+') VIRTUAL')
                    parts.append(quote(generated))
        else:parts=[quote(p['name']) for p in index['parts']]
        if predicate:
            generated='_ml_pred_'+hashlib.sha256(index['name'].encode()).hexdigest()[:16]
            extra.append(quote(generated)+' TINYINT GENERATED ALWAYS AS (CASE WHEN '+predicate.this.sql(dialect='mysql',identify=True)+' THEN 1 ELSE NULL END) VIRTUAL')
            parts.append(quote(generated))
        indexes.append(('UNIQUE ' if index['unique'] else '')+'INDEX '+quote(index['name'].replace('sqlite_autoindex_','auto_'))+' ('+','.join(parts)+')')
    return 'CREATE TABLE '+quote(name)+' ('+','.join(definitions+extra+indexes)+') ENGINE=InnoDB',definitions,extra,indexes


def initialize(target,schema=None):
    connection=Connection(target)
    try:
        if schema is None:
            try:
                cursor=connection.raw.cursor();cursor.execute('SELECT payload FROM `_matchlab_migration` WHERE id=1');marker=cursor.fetchone()
                marker=json.loads(marker[0]) if marker else {}
            except Exception:marker={}
            if marker.get('format')!='matchlab.mysql-migration.v1' or marker.get('state')!='verified':
                raise ValueError('MySQL не содержит проверенной миграции Matchlab. Переключение на пустую/постороннюю базу запрещено.')
        for table in schema or template_schema():
            cursor=connection.raw.cursor();cursor.execute('SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=%s',(table['name'],))
            if not cursor.fetchone()[0]:cursor.execute(schema_statements(table)[0])
            else:
                cursor.execute('SELECT column_name FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name=%s',(table['name'],));names={r[0] for r in cursor.fetchall()}
                _,definitions,extra,indexes=schema_statements(table)
                for column,definition in zip(table['columns'],definitions):
                    if column['name'] not in names:cursor.execute('ALTER TABLE '+quote(table['name'])+' ADD COLUMN '+definition)
                for definition in extra:
                    if definition.split('`')[1] not in names:cursor.execute('ALTER TABLE '+quote(table['name'])+' ADD COLUMN '+definition)
                cursor.execute('SELECT DISTINCT index_name FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name=%s',(table['name'],));existing={r[0] for r in cursor.fetchall()}
                for definition in indexes:
                    if definition.split('`')[1] not in existing:cursor.execute('ALTER TABLE '+quote(table['name'])+' ADD '+definition)
        if schema is None:
            updated=merge_schema(marker['schema'],template_schema())
            if updated!=marker['schema']:
                marker['schema']=updated
                connection.raw.cursor().execute('UPDATE `_matchlab_migration` SET payload=%s WHERE id=1',(json.dumps(marker,ensure_ascii=False),))
        ensure_verdict_projection(connection)
    finally:connection.close()


def ensure_verdict_projection(connection):
    # STORED generation parses the large snapshot once on write, not eleven
    # times per historical row on every results/picks page. Raw UPDATEs also
    # regenerate it atomically, so there is no stale side cache to grade from.
    from .verdict_projection import MYSQL_COLUMN,mysql_definition
    cursor=connection.raw.cursor()
    lock_name=connection.lock_name.replace('matchlab-write-','matchlab-schema-',1)
    cursor.execute('SELECT GET_LOCK(%s,120)',(lock_name,))
    if cursor.fetchone()[0]!=1:raise ValueError('Could not acquire the report-schema preparation lock')
    try:
        cursor.execute("SELECT column_name,extra FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='event_analyses'")
        columns=dict(cursor.fetchall())
        if not columns:return
        if MYSQL_COLUMN not in columns:
            cursor.execute('ALTER TABLE `event_analyses` ADD COLUMN '+mysql_definition())
        elif 'STORED GENERATED' not in columns[MYSQL_COLUMN]:
            raise ValueError('Report projection must be an automatically stored generated column')
        from .report_queries import START_COLUMN,RECEIVED_COLUMN,time_definition
        cursor.execute("SELECT column_name,extra FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='archive_latest_results'")
        columns=dict(cursor.fetchall())
        if not columns:return
        for name,source in ((START_COLUMN,'start_at'),(RECEIVED_COLUMN,'received_at')):
            if name not in columns:cursor.execute('ALTER TABLE `archive_latest_results` ADD COLUMN '+time_definition(name,source))
            elif 'STORED GENERATED' not in columns[name]:raise ValueError('Archive time projection must be automatically generated')
        cursor.execute("SELECT DISTINCT index_name FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name='archive_latest_results'")
        indexes={r[0] for r in cursor.fetchall()}
        for name,fields in (('ml_result_fixture_time_v1','`sport_id`,`'+START_COLUMN+'`'),('ml_result_delivery_time_v1','`'+RECEIVED_COLUMN+'`'),
                ('ml_result_sport_date_v1','`sport_id`,`start_at`')):
            if name not in indexes:cursor.execute('ALTER TABLE `archive_latest_results` ADD INDEX `'+name+'` ('+fields+')')
    finally:cursor.execute('SELECT RELEASE_LOCK(%s)',(lock_name,))


def merge_schema(saved,current):
    """Track additive app migrations so a future backup includes every field."""
    from sqlglot import parse_one,exp
    with sqlite3.connect(':memory:') as db:
        db.row_factory=sqlite3.Row
        for table in saved:
            db.execute(table['sql'])
            for index in table['indexes']:
                if index['sql']:db.execute(index['sql'])
        for table in current:
            exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table['name'],)).fetchone()
            if not exists:db.execute(table['sql'])
            else:
                names={r['name'] for r in db.execute('PRAGMA table_info('+quote(table['name'])+')')}
                for column in parse_one(table['sql'],read='sqlite').find_all(exp.ColumnDef):
                    if column.name not in names:db.execute('ALTER TABLE '+quote(table['name'])+' ADD COLUMN '+column.sql(dialect='sqlite',identify=True))
            for index in table['indexes']:
                if index['sql'] and not db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(index['name'],)).fetchone():db.execute(index['sql'])
        return inspect_sqlite(db)


@contextmanager
def readonly(database):
    if isinstance(database,MySQLTarget):
        connection=Connection(database,readonly=True)
    else:
        from pathlib import Path
        from urllib.parse import quote as uri_quote
        connection=sqlite3.connect('file:'+uri_quote(str(Path(database).resolve()))+'?mode=ro',uri=True)
        connection.row_factory=sqlite3.Row
    try:yield connection
    finally:connection.close()
