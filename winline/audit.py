#!/usr/bin/env python3
"""Export a reproducible coverage report from the local database."""

import argparse
import json
from pathlib import Path

try:
    from .query import read_query
    from .storage import DEFAULT_DB, now
except ImportError:
    from query import read_query
    from storage import DEFAULT_DB, now


def audit(database):
    def query(sql, parameters=()):
        return read_query(sql, parameters, database)

    return {
        "checked_at": now(),
        "integrity": query("PRAGMA quick_check"),
        "latest_snapshot": query("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1"),
        "runs": query("""SELECT r.capture_id,r.status,r.sdk_version,
          r.planned_market_requests,r.planned_result_requests,c.started_at,c.finished_at,c.error
          FROM collection_runs r JOIN captures c ON c.id=r.capture_id
          ORDER BY r.capture_id DESC"""),
        "source_market_count_comparison": query("""SELECT count(*) AS current_prematch,
          sum(CASE WHEN json_extract(e.raw_json,'$.count_add_line') >
            (SELECT count(*) FROM lines l WHERE l.event_id=e.id AND l.is_live=0
             AND l.snapshot_id=e.snapshot_id) THEN 1 ELSE 0 END) AS events_below_advertised_count
          FROM events e WHERE is_live=0 AND snapshot_id=(SELECT max(id) FROM snapshots)"""),
        "markets_by_run": query("""SELECT capture_id,count(*) attempted,
          sum(error IS NULL) successful,sum(error IS NOT NULL) failed,
          sum(row_count) returned_lines FROM market_requests GROUP BY capture_id"""),
        "results_by_run": query("""SELECT capture_id,count(*) attempted,
          sum(error IS NULL) successful,sum(error IS NOT NULL) failed,
          sum(row_count) returned_rows FROM result_requests GROUP BY capture_id"""),
        "supplemental": query("""SELECT kind,count(*) AS event_requests,
          sum(json_array_length(raw_json)) AS returned_records FROM supplemental GROUP BY kind"""),
        "result_range": query("""SELECT count(*) matches_count,min(start_at) oldest,
          max(start_at) newest,count(DISTINCT sport_id) sports_count FROM results"""),
        "note": (
            "Fullness is limited to public SDK collections and announced result dates. "
            "Advertised player/outright modes are stored separately in supplemental. "
            "Odds before the start of collection are not available. "
            "Snapshot presence is not a guarantee of server quote freshness."
        ),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--database", type=Path, default=DEFAULT_DB)
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    text = json.dumps(audit(args.database), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
