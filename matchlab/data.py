import csv
from datetime import date, datetime
import hashlib
import io
from pathlib import Path
import re

import httpx

from .storage import Repository

PRIMARY = "https://www.football-data.co.uk/mmz4281/{season}/E0.csv"
MIRROR = "https://raw.githubusercontent.com/datasets/football-datasets/master/datasets/premier-league/season-{season}.csv"


def validate_season(season: str) -> str:
    if not re.fullmatch(r"\d{4}", season) or (int(season[:2]) + 1) % 100 != int(season[2:]):
        raise ValueError("Сезон должен иметь формат 2425 (2024/25).")
    return season


def parse_csv(content: bytes, season: str) -> list[dict]:
    validate_season(season)
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1252")
    reader = csv.DictReader(io.StringIO(text))
    required = {"Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"}
    if not required.issubset(reader.fieldnames or []):
        raise ValueError("CSV должен содержать Date, HomeTeam, AwayTeam, FTHG, FTAG.")
    rows, seen = [], set()
    start_year = 2000 + int(season[:2])
    for line, item in enumerate(reader, start=2):
        if not (item.get("Date") or "").strip():
            continue
        if not str(item.get("FTHG") or "").strip() or not str(item.get("FTAG") or "").strip():
            continue  # Fixtures without final scores are not training examples.
        try:
            value = item["Date"].strip()
            played = None
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
                try:
                    played = datetime.strptime(value, fmt).date()
                    break
                except ValueError:
                    pass
            if played is None:
                raise ValueError("неверная дата")
            if not date(start_year, 7, 1) <= played < date(start_year + 1, 7, 1):
                raise ValueError("дата не соответствует сезону")
            if played > date.today():
                raise ValueError("результат матча из будущего")
            home, away = item["HomeTeam"].strip(), item["AwayTeam"].strip()
            hg, ag = int(item["FTHG"]), int(item["FTAG"])
            if not home or not away or home == away or not (0 <= hg <= 30 and 0 <= ag <= 30):
                raise ValueError("неверные команды или счёт")
            outcome = "H" if hg > ag else "A" if ag > hg else "D"
            if item.get("FTR", "").strip() not in ("", outcome):
                raise ValueError("исход не соответствует счёту")
            key = (played.isoformat(), home, away)
            if key in seen:
                raise ValueError("повтор матча")
            seen.add(key)
            rows.append(dict(date=key[0], season=season, home=home, away=away, home_goals=hg, away_goals=ag))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError(f"CSV, строка {line}: {exc}") from exc
    return rows


def import_file(repo: Repository, path: Path, season: str) -> int:
    content = path.read_bytes()
    rows = parse_csv(content, season)
    repo.replace_season(season, rows, "local CSV", hashlib.sha256(content).hexdigest())
    return len(rows)


def download_seasons(repo: Repository, raw_dir: Path, seasons: list[str]) -> list[dict]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    results = []
    primary_blocked = False
    with httpx.Client(timeout=25, follow_redirects=True, headers={"User-Agent": "Matchlab/0.1"}) as client:
        for season in seasons:
            validate_season(season)
            errors = []
            templates = [MIRROR] if primary_blocked else [PRIMARY, MIRROR]
            for template in templates:
                url = template.format(season=season)
                try:
                    response = client.get(url)
                    response.raise_for_status()
                    content = response.content
                    rows = parse_csv(content, season)
                    digest = hashlib.sha256(content).hexdigest()
                    repo.replace_season(season, rows, url, digest)
                    (raw_dir / f"E0-{season}-{digest[:12]}.csv").write_bytes(content)
                    results.append(dict(season=season, status="imported", matches=len(rows), source=url))
                    break
                except (httpx.HTTPError, ValueError) as exc:
                    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                    errors.append(f"{'primary' if template == PRIMARY else 'mirror'}: HTTP {status}" if status else f"{'primary' if template == PRIMARY else 'mirror'}: {type(exc).__name__}")
                    if template == PRIMARY and (status in (401, 403) or isinstance(exc, httpx.ProxyError)):
                        primary_blocked = True
            else:
                results.append(dict(season=season, status="unavailable", errors=errors))
    return results
