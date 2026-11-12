#!/usr/bin/env python3
"""Configure only the Matchlab virtual host on an existing Nginx."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import http.client
from uuid import uuid4

MARKER='# Managed by Matchlab HTTP domain'


def domain_name(value):
    value=value.lower().rstrip('.')
    if len(value)>253 or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}',value):
        raise ValueError('Нужно доменное имя без схемы, пути и специальных символов.')
    return value


def configuration(domain,port,cert=None,key=None,acme_root=None):
    zone='matchlab_'+hashlib.sha256(domain.encode()).hexdigest()[:12]
    auth_zone='matchlab_auth_'+hashlib.sha256(domain.encode()).hexdigest()[:12]
    tls=(f'    listen 443 ssl;\n    listen [::]:443 ssl;\n    ssl_certificate {cert};\n    ssl_certificate_key {key};\n' if cert and key else '')
    acme=(f'''    # Managed by Matchlab ACME challenge
    location ^~ /.well-known/acme-challenge/ {{
        root {acme_root};
        try_files $uri =404;
    }}
''' if acme_root else '')
    proxy=f'''        proxy_pass http://127.0.0.1:{port};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Matchlab-Collector-Key "";
        proxy_connect_timeout 5s;
        proxy_read_timeout 180s;'''
    return f'''{MARKER}
# HTTP and optional HTTPS for this virtual host; no global defaults changed.
limit_req_zone $binary_remote_addr zone={zone}:1m rate=6r/m;
limit_req_zone $binary_remote_addr zone={auth_zone}:1m rate=20r/m;
server {{
    listen 80;
    listen [::]:80;
{tls.rstrip()}
    server_name {domain};
{('    if ($scheme = http) { return 308 https://'+domain+'$request_uri; }') if cert and key else ''}
    client_max_body_size 2m;
    add_header X-Matchlab-Site {domain} always;

{acme.rstrip()}
    # The worker API remains accessible only through the existing SSH tunnel.
    location ~ ^/api/bookmakers/(ingest|worker-status|events/ingest|statistics/(queue|ingest)|refresh/(claim|finish)|archive/(ingest|watch))/?$ {{
        return 404;
    }}
    location ~ ^/api/(explain|forecast|bookmakers/events/[0-9]+/explain|archive/events/[0-9]+/explain)/?$ {{
        limit_req zone={zone} burst=2 nodelay;
        limit_req_status 429;
{proxy}
    }}
    location ~ ^/api/(auth/(login|register)|admin/reauth)/?$ {{
        limit_req zone={auth_zone} burst=10 nodelay;
        limit_req_status 429;
{proxy}
    }}
    # Managed by Matchlab social callback privacy
    location ~ ^/api/auth/social/(yandex|vkid)/callback$ {{
        access_log off;
{proxy}
    }}
    location / {{
{proxy}
    }}
}}
'''


def command(arguments):
    result=subprocess.run(arguments,capture_output=True,text=True,check=False)
    if result.returncode:
        raise RuntimeError('Команда '+arguments[0]+' не выполнена: '+result.stderr.strip()[:1200])
    return result.stdout


def validate_tls(domain,cert,key):
    """Check public certificate identity, expiry and key match without printing key material."""
    for value in (cert,key):
        if not value or not re.fullmatch(r'/[A-Za-z0-9_./-]+',value) or not Path(value).is_file():
            raise ValueError('Нужны существующие абсолютные пути к сертификату и закрытому ключу без пробелов.')
    try:
        check=subprocess.run(['openssl','x509','-in',cert,'-noout','-checkhost',domain],check=True,capture_output=True,text=True,timeout=10)
        if check.stdout.strip()!=f'Hostname {domain} does match certificate':
            raise ValueError('Сертификат не выдан для этого домена.')
        subprocess.run(['openssl','x509','-in',cert,'-noout','-checkend','86400'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
        public=subprocess.run(['openssl','x509','-in',cert,'-pubkey','-noout'],check=True,capture_output=True,timeout=10).stdout
        private_public=subprocess.run(['openssl','pkey','-in',key,'-passin','pass:','-pubout'],check=True,capture_output=True,timeout=10).stdout
        if not public or public!=private_public: raise ValueError('Сертификат и закрытый ключ не соответствуют друг другу.')
    except (ssl.SSLError,ssl.CertificateError,subprocess.SubprocessError,OSError) as error:
        raise ValueError('Не удалось проверить домен, срок действия сертификата или его закрытый ключ.') from error


def probe_https(domain,*,port=443,context=None):
    """Verify local TLS, hostname, SNI and the Matchlab response without DNS or proxies."""
    context=context or ssl.create_default_context()
    with socket.create_connection(('127.0.0.1',port),timeout=3) as connection:
        with context.wrap_socket(connection,server_hostname=domain) as secured:
            secured.sendall(f'GET /api/bookmakers/events?limit=1 HTTP/1.1\r\nHost: {domain}\r\nConnection: close\r\n\r\n'.encode())
            response=http.client.HTTPResponse(secured)
            response.begin()
            body=json.loads(response.read(1024*1024))
            if response.status!=200 or response.getheader('X-Matchlab-Site')!=domain or not isinstance(body,dict) or not isinstance(body.get('events'),list):
                raise ValueError('HTTPS ответил не как сайт Matchlab.')


def prevent_tls_downgrade(available,cert):
    if available.exists() and not cert and re.search(r'\blisten\s+[^;]*\b443\b',available.read_text()):
        raise ValueError('На этом сайте уже настроен HTTPS: укажите существующие --tls-cert и --tls-key, чтобы не удалить его.')


def prevent_acme_downgrade(available,acme_root):
    if available.exists() and not acme_root and '# Managed by Matchlab ACME challenge' in available.read_text():
        raise ValueError('На этом сайте включено продление сертификата: укажите существующий --acme-webroot.')


def validate_acme_root(value):
    if not value or not re.fullmatch(r'/[A-Za-z0-9_./-]+',value) or not Path(value).is_dir():
        raise ValueError('Нужен существующий абсолютный каталог ACME без пробелов.')


def probe_acme(domain,root,*,port=80):
    folder=Path(root)/'.well-known'/'acme-challenge'
    if not folder.is_dir(): raise ValueError('Нет каталога .well-known/acme-challenge в ACME webroot.')
    token='matchlab-check-'+uuid4().hex
    path=folder/token
    created=False
    try:
        with path.open('x',encoding='ascii') as output:
            created=True; os.fchmod(output.fileno(),0o644); output.write(token)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request=urllib.request.Request(f'http://127.0.0.1:{port}/.well-known/acme-challenge/'+token,headers={'Host':domain})
        with opener.open(request,timeout=3) as response:
            if response.status!=200 or response.headers.get('X-Matchlab-Site')!=domain or response.read(200).decode('ascii')!=token:
                raise ValueError('ACME проверка пришла не от отдельного сайта Matchlab.')
    finally:
        if created: path.unlink(missing_ok=True)


def conflicts(dump,domain,owned):
    files={}; current='unknown'
    for line in dump.splitlines():
        match=re.fullmatch(r'# configuration file (.+):',line)
        if match: current=match[1]; files.setdefault(current,[])
        else: files.setdefault(current,[]).append(line.split('#',1)[0])
    return [path for path,lines in files.items() if path!=str(owned)
            and any(domain in declaration.lower().split() for declaration in re.findall(r'\bserver_name\s+([^;]+);','\n'.join(lines))) ]


def check_paths(available,enabled):
    if available.is_symlink() or (available.exists() and available.read_text().splitlines()[:1]!=[MARKER]):
        raise ValueError('Файл сайта занят другой конфигурацией: '+str(available))
    if enabled.is_symlink():
        if enabled.resolve()!=available.resolve(): raise ValueError('Ссылка сайта уже ведёт на другой проект.')
    elif enabled.exists():
        raise ValueError('В sites-enabled уже есть другой файл с таким именем.')


def write_atomic(path,data):
    with tempfile.NamedTemporaryFile('wb',dir=path.parent,delete=False) as output:
        temporary=Path(output.name); output.write(data); output.flush(); os.fsync(output.fileno())
    try:
        temporary.chmod(0o644)
        temporary.replace(path)
    finally: temporary.unlink(missing_ok=True)


def apply_site(available,enabled,data,*,run=command,verify=None):
    check_paths(available,enabled)
    previous=available.read_bytes() if available.exists() else None
    had_link=enabled.is_symlink()
    try:
        write_atomic(available,data.encode())
        if not had_link: enabled.symlink_to(available)
        run(['nginx','-t'])
        run(['systemctl','reload','nginx.service'])
        if verify: verify()
    except Exception as error:
        if not had_link and enabled.is_symlink(): enabled.unlink()
        if previous is None: available.unlink(missing_ok=True)
        else: write_atomic(available,previous)
        try:
            run(['nginx','-t'])
            run(['systemctl','reload','nginx.service'])
        except Exception:
            raise RuntimeError('Файлы сайта восстановлены, но проверка/перечитывание прежней конфигурации не удалось. Проверьте sudo nginx -t; Nginx не останавливался.') from error
        raise RuntimeError('Изменения сайта отменены; прежняя конфигурация восстановлена. '+str(error)) from error


def probe(url,domain=None):
    headers={'Host':domain} if domain else {}
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url,headers=headers),timeout=3) as response:
        data=json.load(response)
        if response.status!=200 or not isinstance(data,dict) or not isinstance(data.get('events'),list):
            raise ValueError('На порту приложения не найден каталог Matchlab.')
        if domain and response.headers.get('X-Matchlab-Site')!=domain:
            raise ValueError('Ответ пришёл от другого виртуального хоста.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--domain',default='prognozich.online')
    parser.add_argument('--port',type=int,default=8080)
    parser.add_argument('--tls-cert',help='Absolute path to the existing full certificate chain on this VPS')
    parser.add_argument('--tls-key',help='Absolute path to the matching private key on this VPS')
    parser.add_argument('--acme-webroot',help='Existing absolute webroot directory for HTTP-01 certificate renewal')
    parser.add_argument('--check',action='store_true',help='Read-only preflight and configuration preview')
    args=parser.parse_args()
    try:
        domain=domain_name(args.domain)
        if not 1<=args.port<=65535: raise ValueError('Некорректный порт приложения.')
        if os.geteuid()!=0: raise ValueError('Запустите через sudo на VPS с Nginx.')
        if bool(args.tls_cert)!=bool(args.tls_key): raise ValueError('Нужно указать и --tls-cert, и --tls-key.')
        if args.tls_cert: validate_tls(domain,args.tls_cert,args.tls_key)
        if args.acme_webroot: validate_acme_root(args.acme_webroot)
        available=Path('/etc/nginx/sites-available')/('matchlab-'+domain+'.conf')
        enabled=Path('/etc/nginx/sites-enabled')/available.name
        if not available.parent.is_dir() or not enabled.parent.is_dir():
            raise ValueError('Ожидалась существующая Debian/Ubuntu-конфигурация sites-available/sites-enabled.')
        command(['systemctl','is-active','--quiet','nginx.service'])
        dump=command(['nginx','-T'])
        if not re.search(r'''\binclude\s+["']?/etc/nginx/sites-enabled/\*["']?\s*;''',dump):
            raise ValueError('Nginx не подключает стандартный sites-enabled; его основная конфигурация не менялась.')
        occupied=conflicts(dump,domain,enabled)
        if occupied: raise ValueError('Домен уже объявлен в другой конфигурации: '+', '.join(occupied))
        check_paths(available,enabled)
        prevent_tls_downgrade(available,args.tls_cert)
        prevent_acme_downgrade(available,args.acme_webroot)
        probe(f'http://127.0.0.1:{args.port}/api/bookmakers/events?limit=1')
        data=configuration(domain,args.port,args.tls_cert,args.tls_key,args.acme_webroot)
        if args.check:
            print(data)
            print('Проверка выполнена без изменений. '+('Сертификат и ключ проверены.' if args.tls_cert else 'SSL и порт 443 не настраиваются.'))
            return
        if available.exists():
            backup=Path('/var/backups/matchlab/nginx')
            backup.mkdir(parents=True,exist_ok=True,mode=0o700)
            stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
            with (backup/(available.name+'.'+stamp)).open('xb') as output:
                os.fchmod(output.fileno(),0o600); output.write(available.read_bytes())
        def verify():
            for _ in range(10):
                try:
                    probe('http://127.0.0.1/api/bookmakers/events?limit=1',domain)
                    if args.acme_webroot: probe_acme(domain,args.acme_webroot)
                    if args.tls_cert: probe_https(domain)
                    return
                except (OSError,ValueError,http.client.HTTPException): time.sleep(.3)
            raise ValueError('После reload новый виртуальный хост не ответил как Matchlab.')
        apply_site(available,enabled,data,verify=verify)
    except (OSError,ValueError,RuntimeError) as error:
        parser.exit(1,str(error)+'\n')
    print('Готово: '+('https' if args.tls_cert else 'http')+'://'+domain+' → 127.0.0.1:'+str(args.port))
    print('Изменён только отдельный сайт, Nginx проверен и перечитан без остановки. Чужие файлы не менялись.')


if __name__=='__main__': main()
