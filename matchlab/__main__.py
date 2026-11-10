import argparse
from contextlib import nullcontext
from datetime import date
import json
from pathlib import Path
import sys

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
    sub.add_parser("check-qwen", help="Check /models and a real completion without exposing the API key")
    sub.add_parser('check-qwen-load',help='Time a tiny request and one bounded match sample; never print its content')
    sub.add_parser('inspect-qwen-context',help='Count the full saved match context without starting generation')
    comparison=sub.add_parser('compare-qwen-context',help='Compare full and relevant markets from one saved snapshot; tokenize only')
    comparison.add_argument('--market-limit',type=int,default=None)
    bookmaker_import = sub.add_parser("import-bookmakers", help="Append validated 90-minute football snapshots from a capture or normalized JSON")
    bookmaker_import.add_argument("file", nargs="?", type=Path)
    bookmaker_import.add_argument("--stdin", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_env()
    try:
        if args.command == "check-qwen":
            from .qwen import check_connection
            print(json.dumps(check_connection(settings), ensure_ascii=False, indent=2))
            return
        if args.command=='check-qwen-load':
            from .qwen_probe import check_load
            print(json.dumps(check_load(settings),ensure_ascii=False,indent=2))
            return
        if args.command=='inspect-qwen-context':
            from .qwen_probe import inspect_context
            print(json.dumps(inspect_context(settings),ensure_ascii=False,indent=2))
            return
        if args.command=='compare-qwen-context':
            from .qwen_probe import compare_context
            print(json.dumps(compare_context(settings,args.market_limit),ensure_ascii=False,indent=2))
            return
        repo = Repository(settings.database)
        repo.initialize()
        if args.command == "import-bookmakers":
            if bool(args.file) == args.stdin:
                parser.error("Укажите файл или --stdin.")
            with args.file.open("rb") if args.file else nullcontext(sys.stdin.buffer) as stream:
                content = stream.read(16 * 1024 * 1024 + 1)
            if len(content) > 16 * 1024 * 1024:
                raise ValueError("Файл больше 16 MiB. Используйте нормализованный экспорт.")
            print(json.dumps(repo.import_bookmakers(json.loads(content)), ensure_ascii=False, indent=2))
        elif args.command == "download":
            results = download_seasons(repo, settings.data_dir / "raw", args.seasons)
            print(json.dumps(results, ensure_ascii=False, indent=2))
            if any(result["status"] != "imported" for result in results):
                # A partial download is reported honestly, including missing
                # current-season feeds. Successfully imported data is retained.
                raise SystemExit(2)
        elif args.command == "import-csv":
            print(json.dumps({"imported": import_file(repo, args.file, args.season)}, ensure_ascii=False))
        elif args.command == "backtest":
            from .backtest import run_backtest, save_report
            report = run_backtest(repo, args.seasons)
            save_report(report, settings.report)
            print(json.dumps({k: report[k] for k in ("model", "baseline", "selected_matches", "skipped_matches", "profitability_verified")}, ensure_ascii=False, indent=2))
        elif args.command == "status":
            print(json.dumps(repo.status(), ensure_ascii=False, indent=2))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Ошибка: {exc}\n")


if __name__ == "__main__":
    main()
