#!/usr/bin/env python3
"""Add a separate reverse tunnel using the existing managed scraper SSH identity."""
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

SOURCE = Path('/etc/systemd/system/matchlab-scraper-tunnel.service')
TARGET = Path('/etc/systemd/system/matchlab-wigolo-tunnel.service')
TOKEN = Path('/var/lib/matchlab-wigolo/api-token')
MARKER = '# Managed by Matchlab Wigolo integration'


def fail(message):
    raise SystemExit(message)


def main():
    if os.geteuid() != 0 or len(sys.argv) != 1:
        fail('Запустите без аргументов через sudo на сервере нейросети.')
    if SOURCE.is_symlink() or not SOURCE.is_file():
        fail('Не найден управляемый туннель сборщика; потребуется настроить отдельный SSH-туннель вручную.')
    text = SOURCE.read_text()
    if not text.startswith('# Managed by Matchlab server scraper\n'):
        fail('Конфигурация туннеля отличается от управляемой; изменений нет.')
    dropins = subprocess.run(['systemctl', 'show', 'matchlab-scraper-tunnel.service', '-p', 'DropInPaths', '--value'], capture_output=True, text=True, check=True).stdout.strip()
    if dropins:
        fail('У туннеля есть дополнительные настройки. Нужна ручная проверка; изменений нет.')
    users = re.findall(r'^User=([a-z_][a-z0-9_-]*)$', text, re.MULTILINE)
    commands = re.findall(r'^ExecStart=(.*)$', text, re.MULTILINE)
    if len(users) != 1 or len(commands) != 1:
        fail('Не удалось однозначно определить пользователя и SSH-команду.')
    args = shlex.split(commands[0])
    if len(args) < 5 or args[0:2] != ['/usr/bin/ssh', '-NT'] or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]*', args[-1]):
        fail('Неожиданная SSH-команда; изменений нет.')
    options, index, forward = [], 2, False
    allowed = {'BatchMode=yes', 'IdentityAgent=none', 'StrictHostKeyChecking=yes', 'ControlMaster=no',
               'ControlPath=none', 'ConnectTimeout=10', 'ExitOnForwardFailure=yes',
               'ServerAliveInterval=30', 'ServerAliveCountMax=3', 'IdentitiesOnly=yes'}
    while index < len(args)-1:
        if index + 1 >= len(args)-1:
            fail('Неполная SSH-команда.')
        flag, value = args[index:index+2]
        if flag == '-L' and value == '127.0.0.1:18081:127.0.0.1:8080' and not forward:
            forward = True
        elif flag == '-o' and value in allowed or flag == '-i' and re.fullmatch(r'/[A-Za-z0-9_./@+-]+', value):
            options += [flag, value]
        else:
            fail('SSH-команда содержит неизвестную настройку; изменений нет.')
        index += 2
    if not forward or TOKEN.is_symlink() or not TOKEN.is_file():
        fail('Нужен установленный Wigolo и стандартный туннель сборщика.')
    if not {'BatchMode=yes', 'IdentityAgent=none', 'StrictHostKeyChecking=yes', 'ExitOnForwardFailure=yes'}.issubset(set(options)):
        fail('В существующей SSH-команде отсутствуют обязательные параметры безопасности.')
    token = TOKEN.read_bytes()
    if not re.fullmatch(rb'[0-9a-f]{64}\n?', token):
        fail('Некорректный файл токена Wigolo.')
    if TARGET.is_symlink() or (TARGET.exists() and not TARGET.read_text().startswith(MARKER + '\n')):
        fail('Имя службы Wigolo уже занято; изменений нет.')
    remote = '''set -eu
umask 077
test "$(id -u)" = 0
tmp=$(mktemp /etc/.matchlab-wigolo-token.XXXXXX)
trap 'rm -f "$tmp"' EXIT
cat > "$tmp"
if test -e /etc/matchlab-wigolo-token; then
  test ! -L /etc/matchlab-wigolo-token && test "$(stat -c '%u:%a' /etc/matchlab-wigolo-token)" = 0:600 && cmp -s "$tmp" /etc/matchlab-wigolo-token || exit 1
else
  install -m 600 "$tmp" /etc/matchlab-wigolo-token
fi
'''
    command = ['runuser', '-u', users[0], '--', '/usr/bin/ssh', *options, args[-1], 'sh -c ' + shlex.quote(remote)]
    transferred = subprocess.run(command, input=token, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
    if transferred.returncode:
        fail('Не удалось безопасно передать токен по существующему SSH-доступу или на VPS уже лежит другой токен. Проверьте SSH; значение токена не выводите.')
    ssh = ['/usr/bin/ssh', '-NT', *options, '-R', '127.0.0.1:13333:127.0.0.1:13334', args[-1]]
    unit = f'''{MARKER}
[Unit]
Description=Private Wigolo MCP reverse SSH tunnel for Matchlab
After=network-online.target matchlab-wigolo.service
Wants=network-online.target

[Service]
Type=simple
User={users[0]}
ExecStart={shlex.join(ssh)}
Restart=always
RestartSec=15
MemoryMax=64M
CPUQuota=10%
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
UMask=0077

[Install]
WantedBy=multi-user.target
'''
    TARGET.write_text(unit)
    os.chmod(TARGET, 0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', TARGET.name], check=True)
    subprocess.run(['systemctl', 'restart', TARGET.name], check=True)
    print('Отдельный туннель запущен. Токен передан на VPS через SSH без вывода в терминал.')
    print('Проверка: sudo systemctl status matchlab-wigolo matchlab-wigolo-tunnel --no-pager')


if __name__ == '__main__':
    main()
