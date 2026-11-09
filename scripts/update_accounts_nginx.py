#!/usr/bin/env python3
"""Enable HTTPS-only accounts and scoped authentication rate limits in our site."""

import argparse
import hashlib
import os
import re
from datetime import UTC, datetime
from pathlib import Path

try:
    from .configure_domain import MARKER, apply_site, check_paths, domain_name
except ImportError:
    from configure_domain import MARKER, apply_site, check_paths, domain_name


def updated(text, domain):
    if text.splitlines()[:1] != [MARKER] or re.findall(
        r"\bserver_name\s+([^;]+);", text
    ) != [domain]:
        raise ValueError("Неожиданная конфигурация сайта. Файл не изменён.")
    if not re.search(r"\blisten\s+443\s+ssl\s*;", text):
        raise ValueError(
            "Для аккаунтов сначала нужен настроенный HTTPS. Сертификаты этим скриптом не создаются."
        )
    marker = "# Managed by Matchlab accounts"
    if marker in text:
        return text
    proxy = re.search(r"(?ms)^    location / \{\n(.*?)\n    \}", text)
    if not proxy or not re.search(r"proxy_pass http://127\.0\.0\.1:\d+;", proxy[1]):
        raise ValueError("Не найден существующий upstream Matchlab.")
    zone = "matchlab_auth_" + hashlib.sha256(domain.encode()).hexdigest()[:12]
    if f"zone={zone}:" not in text:
        text = text.replace(
            MARKER + "\n",
            MARKER
            + f"\nlimit_req_zone $binary_remote_addr zone={zone}:1m rate=20r/m;\n",
            1,
        )
    if "auth/(login|register)|admin/reauth" not in text:
        block = f"""    {marker}
    location ~ ^/api/(auth/(login|register)|admin/reauth)/?$ {{
        limit_req zone={zone} burst=10 nodelay;
        limit_req_status 429;
{proxy[1]}
    }}
"""
        text = text.replace("    location / {", block + "    location / {", 1)
    else:
        text = text.replace("    location / {", f"    {marker}\n    location / {{", 1)
    redirect = f"    if ($scheme = http) {{ return 308 https://{domain}$request_uri; }}"
    if not re.search(r"if\s*\(\$scheme\s*=\s*http\)", text):
        text = text.replace(
            f"    server_name {domain};", f"    server_name {domain};\n{redirect}", 1
        )
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", default="prognozich.online")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    domain = domain_name(args.domain)
    available = Path(f"/etc/nginx/sites-available/matchlab-{domain}.conf")
    enabled = Path("/etc/nginx/sites-enabled") / available.name
    check_paths(available, enabled)
    if not available.is_file() or not enabled.is_symlink():
        parser.error("Нужна существующая включённая конфигурация сайта.")
    before = available.read_text()
    after = updated(before, domain)
    if args.check or before == after:
        print(
            "Проверка пройдена. "
            + ("Правила аккаунтов готовы." if before != after else "Уже обновлено.")
        )
        return
    if os.geteuid() != 0:
        parser.error("Запустите через sudo на VPS.")
    backup = available.with_name(
        available.name
        + ".accounts-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        + ".bak"
    )
    backup.write_text(before)
    backup.chmod(0o600)
    apply_site(available, enabled, after)
    print(f"HTTPS и лимиты входа обновлены. Копия: {backup}")


if __name__ == "__main__":
    main()
