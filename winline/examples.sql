-- Текущие события по видам спорта.
SELECT sport, is_live, count(*) AS matches_count
FROM matches WHERE present_in_latest_snapshot=1 GROUP BY sport,is_live;

-- Доступные коэффициенты одного события: заменить ID на актуальный.
SELECT line_id,market_id,market_template,parameter,odds_json,labels_json,last_seen
FROM latest_odds WHERE event_id=17119181 AND present_in_latest_snapshot=1;

-- Исполненные и запланированные запросы последнего запуска.
SELECT r.capture_id,r.status,r.planned_market_requests,r.planned_result_requests,
  (SELECT count(*) FROM market_requests m WHERE m.capture_id=r.capture_id) AS market_done,
  (SELECT count(*) FROM result_requests q WHERE q.capture_id=r.capture_id) AS results_done
FROM collection_runs r ORDER BY r.capture_id DESC LIMIT 1;

-- Итоговые счета в архиве.
SELECT sport_id,start_at,championship,member1,member2,score
FROM results ORDER BY start_at DESC LIMIT 100;

-- История одной линии (позиции V — исходы в исходном порядке сайта).
SELECT observed_at,json_extract(raw_json,'$.V') AS odds
FROM line_history WHERE event_id=17119181 AND line_id=1577311450 ORDER BY id;

-- Коэффициенты рынков игроков из отдельной выгрузки.
SELECT s.event_id,json_extract(j.value,'$.playerName') AS player_name,
  json_extract(j.value,'$.idTipMarket') AS market_id,
  json_extract(j.value,'$.V') AS odds
FROM supplemental s,json_each(s.raw_json) j WHERE s.kind='players';
