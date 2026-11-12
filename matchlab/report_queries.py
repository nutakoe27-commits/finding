"""Indexed archive reads for settlement; retain exact Python identity checks."""
from datetime import timedelta

from .bookmakers import instant

START_COLUMN='_ml_result_start_jd_v1'
RECEIVED_COLUMN='_ml_result_received_jd_v1'


def time_definition(column,source):
    # Match the adapter's UTC/millisecond semantics, but never try an invalid
    # fractional-seconds format first: strict MySQL rejects even a failed
    # COALESCE branch while generating stored values on INSERT/UPDATE.
    value='CAST(`'+source+'` AS CHAR CHARACTER SET utf8mb4)'
    fraction="RPAD(LEFT(SUBSTRING_INDEX(SUBSTRING_INDEX(SUBSTRING_INDEX(SUBSTRING("+value+",21),'+',1),'-',1),'Z',1),6),6,'0')"
    parsed="STR_TO_DATE(CONCAT(LEFT(REPLACE("+value+",'T',' '),19),'.',CASE WHEN SUBSTRING("+value+",20,1)='.' THEN "+fraction+" ELSE '000000' END),'%Y-%m-%d %H:%i:%s.%f')"
    offset="CASE WHEN RIGHT("+value+",6) REGEXP '^[+-][0-9]{2}:[0-9]{2}$' THEN (CASE WHEN LEFT(RIGHT("+value+",6),1)='-' THEN -1 ELSE 1 END)*(CAST(SUBSTRING(RIGHT("+value+",6),2,2) AS SIGNED)*60+CAST(RIGHT("+value+",2) AS SIGNED))/1440.0 ELSE 0 END"
    expression="ROUND(TIMESTAMPDIFF(MICROSECOND,'1970-01-01 00:00:00',"+parsed+")/1000.0)/86400000.0+2440587.5-("+offset+")"
    return ('`'+column+'` DOUBLE GENERATED ALWAYS AS (CASE WHEN '+value+
        " REGEXP '^[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})?$' THEN "+expression+' ELSE NULL END) STORED')


def fixture_windows(fixtures):
    points=sorted({instant(e['kickoff_at']) for e in fixtures})
    windows=[]
    for point in points:
        # The final matching rule remains <=120 seconds in Python. One extra
        # second prevents SQL's millisecond rounding from excluding a boundary.
        low,high=point-timedelta(seconds=121),point+timedelta(seconds=121)
        if windows and low<=windows[-1][1]:windows[-1]=(windows[-1][0],max(high,windows[-1][1]))
        else:windows.append((low,high))
    return windows


def fixture_results(db,sport_id,fixtures):
    windows=fixture_windows(fixtures)
    if not windows:return
    field=START_COLUMN if getattr(db,'dialect',None)=='mysql' else 'julianday(start_at)'
    seen=set()
    for offset in range(0,len(windows),64):
        batch=windows[offset:offset+64]
        predicate=' OR '.join(field+' BETWEEN ? AND ?' for _ in batch)
        # NULL preserves legacy timestamps the indexed representation cannot
        # interpret. Their payloads still pass the original validation below.
        if offset==0:predicate+=' OR '+field+' IS NULL'
        # Convert the few bounds once in Python, not a large SQL expression for
        # every parameter. All are UTC; the one-second margin covers rounding.
        params=[sport_id,*[2440587.5+at.timestamp()/86400 for pair in batch for at in pair]]
        rows=db.execute('SELECT result_key,payload,start_at,sport_label,observed_at FROM archive_latest_results WHERE sport_id=? AND ('+predicate+')',params)
        for row in rows:
            if row['result_key'] not in seen:
                seen.add(row['result_key']);yield row
