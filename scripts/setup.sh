#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m matchlab download --seasons 2021 2122 2223 2324 2425 2526
.venv/bin/python -m matchlab backtest --seasons 2425 2526
