"""Small, transactionally current report fields; never expose a raw prompt."""

MYSQL_COLUMN='_ml_verdict_fields_v1'
PATHS=(
    '$.context.neural_input.event','$.context.event','$.event',
    '$.context.neural_verdict','$.context.ai_generation',
    '$.context.neural_input.as_of','$.context.neural_input.quote',
    '$.context.team_statistics.observed_at','$.context.team_statistics.match_phase',
    '$.context.neural_input.policy.force_skip','$.context.analysis_origin',
)


def sqlite_expression():return 'json_extract(payload,'+','.join("'"+p+"'" for p in PATHS)+')'


def mysql_definition():
    fields=','.join("JSON_EXTRACT(`payload`,'"+p+"')" for p in PATHS)
    return ('`'+MYSQL_COLUMN+'` LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin GENERATED ALWAYS AS ('
        "CASE WHEN JSON_VALID(`payload`) THEN CASE WHEN JSON_TYPE(JSON_EXTRACT(`payload`,'$.context.neural_verdict'))='OBJECT' "
        'THEN JSON_ARRAY('+fields+') ELSE NULL END ELSE NULL END) STORED')
