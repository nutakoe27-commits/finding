# prognozich.online: публичный сайт без Mac

Схема развёртывания:

- VPS `89.111.143.45`: существующее приложение `matchlab.service`, база и отдельный
  HTTP-сайт Nginx `prognozich.online` → `127.0.0.1:8080`.
- Сервер с видеокартами: существующий сервер ИИ, `matchlab-crawler.service`
  для всех обычных видов спорта и отдельный `matchlab-statistics.service`.
- Сборщики доставляют данные по существующему серверному SSH-туннелю
  `matchlab-scraper-tunnel.service`, через `127.0.0.1:18081`.

Браузер и SSH-туннели на Mac для работы сайта не нужны. Небольшой VPS не
получает дополнительный Chromium. База и сохранённые разборы остаются на месте.
Избранное хранится в браузере отдельно для каждого адреса сайта.

Домен имеет A-запись `89.111.143.45`. Настраивается только сам
`prognozich.online`, без автоматического добавления `www`. При наличии файлов
сертификата на VPS скрипт может добавить HTTPS только этому сайту. Сертификат
нужен заранее: скрипт не запускает Certbot и не выпускает его. HTTP остаётся
доступен; автоматическое перенаправление на HTTPS не включается.

## VPS

```bash
cd /root/matchlab
git pull --ff-only origin main
systemctl restart matchlab.service
python3 scripts/configure_domain.py --domain prognozich.online
```

Для предварительного просмотра без изменений: добавьте `--check` к последней
команде. Скрипт проверяет работающий Nginx, исходную конфигурацию и каталог
Matchlab на локальном порту. Если домен уже указан в другой активной конфигурации
(например, панели провайдера), выполнение прекращается до записи файлов.

Чтобы подключить существующий сертификат на VPS, укажите реальные абсолютные
пути к полной цепочке и соответствующему закрытому ключу:

```bash
sudo python3 scripts/configure_domain.py --domain prognozich.online \
  --tls-cert /path/to/fullchain.pem --tls-key /path/to/privkey.pem --check
```

Вместо приведённых обозначений должны быть фактические пути без пробелов.
Команда `--check` проверяет домен сертификата, срок его действия и соответствие
ключа, но не меняет файлы. Для применения запускается та же команда без
`--check`; после проверки `nginx -t` и перечитывания скрипт проверяет HTTPS с
SNI, доверенной цепочкой и ответом именно Matchlab. При ошибке он возвращает
предыдущий файл сайта и повторно перечитывает Nginx. Повторный запуск без
параметров сертификата откажется убирать уже настроенный HTTPS.

### Если сертификат есть только в панели хостинга

Можно выпустить отдельный сертификат Let’s Encrypt для этого домена через
работающий HTTP, не останавливая Nginx и не изменяя сертификаты других сайтов.
Сначала подготовьте отдельный webroot и включите его в управляемом сайте:

```bash
cd /root/matchlab
git pull --ff-only origin main &&
sudo bash scripts/prepare_matchlab_acme.sh &&
sudo python3 scripts/configure_domain.py --domain prognozich.online --acme-webroot /var/lib/matchlab-acme --check &&
sudo python3 scripts/configure_domain.py --domain prognozich.online --acme-webroot /var/lib/matchlab-acme
```

После успешной проверки выпустите сертификат через webroot:

```bash
sudo certbot certonly --webroot -w /var/lib/matchlab-acme \
  --cert-name prognozich.online -d prognozich.online --keep-until-expiring
```

Если Certbot сообщает об ошибке, остановитесь и проверьте доступность домена и
порта 80 снаружи. После успешного выпуска включите HTTPS и отдельный hook,
который при обновлении только этого сертификата проверит и перечитает Nginx:

```bash
sudo python3 scripts/configure_domain.py --domain prognozich.online \
  --acme-webroot /var/lib/matchlab-acme \
  --tls-cert /etc/letsencrypt/live/prognozich.online/fullchain.pem \
  --tls-key /etc/letsencrypt/live/prognozich.online/privkey.pem --check &&
sudo python3 scripts/configure_domain.py --domain prognozich.online \
  --acme-webroot /var/lib/matchlab-acme \
  --tls-cert /etc/letsencrypt/live/prognozich.online/fullchain.pem \
  --tls-key /etc/letsencrypt/live/prognozich.online/privkey.pem &&
sudo bash scripts/install_matchlab_certbot_hook.sh
```

Проверьте `curl --resolve prognozich.online:443:127.0.0.1 -I https://prognozich.online/`
и наличие планировщика Certbot: `systemctl list-timers --all | grep -i certbot`;
на некоторых системах вместо таймера используется `/etc/cron.d/certbot`.
HTTP-адрес для проверки выпуска остаётся доступным и при продлении; обычный
запуск скрипта без `--acme-webroot` не удалит эту настройку.

Создаются только собственный файл
`/etc/nginx/sites-available/matchlab-prognozich.online.conf` и ссылка на него
в `sites-enabled`. Перед заменой существующего управляемого файла сохраняется
его копия в `/var/backups/matchlab/nginx`. `nginx -t` предшествует `reload`;
Nginx не останавливается. После перечитывания проверяется ответ именно нового
виртуального хоста с заголовком Host. Ошибка отменяет изменения этого сайта.
Конфигурации остальных доменов не редактируются, `default_server` не меняется.

Публичный сайт не требует пароля. История разборов общая. Дорогие запросы ИИ
и расчёта ограничены на уровне этого сайта шестью запросами в минуту с IP и
допуском двух дополнительных запросов при кратковременном всплеске. Служебные пути приёма данных
и очереди статистики закрыты в публичном Nginx; сборщики пользуются SSH.

Проверка:

```bash
curl -sS -D - -o /dev/null http://prognozich.online/
curl -sS -D - -o /dev/null -H 'Host: prognozich.online' http://127.0.0.1/
systemctl is-active matchlab nginx
```

Ожидается `X-Matchlab-Site: prognozich.online`. Если DNS ещё распространяется,
проверка через Host подтверждает конфигурацию VPS независимо от DNS.

## Сервер сборщика

```bash
cd ~/matchlab-scraper-src
git pull --ff-only origin main
sudo systemctl stop matchlab-crawler.service
sudo bash scripts/install_market_crawler.sh --api-url http://127.0.0.1:18081 --sports all
sudo bash scripts/install_server_statistics.sh
```

Установка статистики повторно использует отдельное Python-окружение сборщика,
браузерный кэш, пользователя `matchlab-scraper`, существующий ключ и серверный
SSH-туннель. Она не получает ключи через Mac и не меняет сервер модели.
Скопированный код статистики находится отдельно в `/opt/matchlab-statistics`.
Служба автоматически запускается после перезагрузки. Лимиты: 1 ГиБ RAM и
два логических ядра CPU. Она открывает статистику только по запросу пользователя
на выбранный матч; задания проверяются примерно раз в две секунды и обрабатываются
последовательно. Время получения зависит от доступности источника и очереди.

```bash
sudo systemctl is-active matchlab-crawler matchlab-statistics matchlab-scraper-tunnel
sudo journalctl -u matchlab-statistics -n 20 --no-pager
sudo cat /var/lib/matchlab-statistics/status.json
```

`state=requested` и `statistics_state=ok` подтверждают доставленную статистику
выбранного матча. `idle` означает, что сейчас нет заданий. `latest_attempt`
содержит результат последней попытки за время работы процесса.
`receiver_unavailable` — проблема приёмника/туннеля/версии API.

Ранее StatsHub на этом сервере возвращал `Access Denied`. Домен сайта этого
ограничения не снимает. Если оно сохраняется, состояние будет `blocked` с паузой
перед повтором, а не ложным успехом. ИИ продолжит разбирать доступные рынки и
покажет отсутствие статистики или время последнего успешного снимка. Для
получения новых данных тогда нужен разрешённый доступ провайдера к серверу либо
отдельный доступный статистический API; Mac в эту схему не входит.

Повторный запрос делается пользователем; после отказа доступа действует часовая пауза.
Подробности привязки и защиты от использования итогов как предматчевых фактов:
[team-statistics.md](team-statistics.md).

## Откат домена

Для отключения только нового сайта:

```bash
sudo unlink /etc/nginx/sites-enabled/matchlab-prognozich.online.conf
sudo nginx -t && sudo systemctl reload nginx
```

Приложение и сборщики при этом продолжают работу. Для остановки только новой
статистики: `sudo systemctl disable --now matchlab-statistics.service`.
