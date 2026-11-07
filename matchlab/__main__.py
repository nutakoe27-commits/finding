import argparse
from datetime import date
import json
from pathlib import Path

from .backtest import run_backtest, save_report
from .config import Settings
from .data import download_seasons, import_file, validate_season
from .storage import Repository


def main():
    parser = argparse.ArgumentParser(description="Matchlab data and evaluation tools")
    sub = parser.add_subparsers(dest="command", required=True)
    download = sub.add_parser("download", help="Download EPL CSVs; keep prior data on failure")
    current = date.today().year - int(date.today().month < 7)
    defaults = [f"{year % 100:02}{(year + 1) % 100:02}" for year in range(current - 6, current + 1)]
    download.add_argument("--seasons", nargs="+", default=defaults, type=validate_season)
    local = sub.add_parser("import-csv")
    local.add_argument("file", type=Path)
    local.add_argument("--season", required=True, type=validate_season)
    backtest = sub.add_parser("backtest")
    backtest.add_argument("--seasons", nargs="+", default=["2425", "2526"], type=validate_season)
    sub.add_parser("status")
    args = parser.parse_args()
    settings = Settings.from_env()
    repo = Repository(settings.database)
    repo.initialize()
    try:
        if args.command == "download":
            results = download_seasons(repo, settings.data_dir / "raw", args.seasons)
            print(json.dumps(results, ensure_ascii=False, indent=2))
            if any(result["status"] != "imported" for result in results):
                # A partial download is reported honestly, including missing
                # current-season feeds. Successfully imported data is retained.
                raise SystemExit(2)
        elif args.command == "import-csv":
            print(json.dumps({"imported": import_file(repo, args.file, args.season)}, ensure_ascii=False))
        elif args.command == "backtest":
            report = run_backtest(repo, args.seasons)
            save_report(report, settings.report)
            print(json.dumps({k: report[k] for k in ("model", "baseline", "selected_matches", "skipped_matches", "profitability_verified")}, ensure_ascii=False, indent=2))
        elif args.command == "status":
            print(json.dumps(repo.status(), ensure_ascii=False, indent=2))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Ошибка: {exc}\n")


if __name__ == "__main__":
    main()
