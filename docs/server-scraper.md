# Автосбор Winline каждые 60 секунд на сервере Qwen

Браузер работает на сервере Qwen (62 ГиБ RAM, 51 ГиБ доступно), готовые
публичные снимки отправляются на VPS в существующий Matchlab. На VPS с
961 МиБ RAM и 198 МиБ доступной памяти Chromium не устанавливается.

```text
Winline → Chromium на сервере Qwen
                            ↓ снимки раз в 60 с
                  127.0.0.1:18081 на сервере Qwen
                            ↓ отдельный SSH-туннель
                  VPS 127.0.0.1:8080 → Matchlab / SQLite
```

Браузерная сессия постоянная, в истории — до одного наблюдения каждого
события за интервал. Собираются видимые карточки предматчевого футбола и
основной рынок П1/Х/П2. Полный каталог/все рынки не заявляются. Браузер ничего
не нажимает, не входит в аккаунт и не обходит CAPTCHA. Доступность сайтов
с IP сервера проверяется перед включением автозапуска.

По умолчанию серверный сборщик использует только Winline и открывает
`https://winline.ru/stavki/sport/futbol/`. BetBoom не опрашивается. Выбор
сохраняется в `/etc/matchlab-scraper.conf` как `BOOKMAKER_SOURCES=winline`.

## 1. Приёмник на VPS

В прежней сессии `root@megotim4y2-vps-1`, каталог `/root/matchlab`:

```bash
cd /root/matchlab
git pull --ff-only origin main
.venv/bin/python scripts/configure_bookmaker_receiver.py
systemctl restart matchlab.service
curl -fsS http://127.0.0.1:8080/api/health
```

Скрипт добавляет только `BOOKMAKER_INGEST_TOKEN` в `.env` и сохраняет копию
ключа в `/etc/matchlab-scraper-token.env` с правами root/600. Перед изменением
`.env` создаётся закрытая резервная копия в `.local/receiver-backups`.
Повторный запуск использует прежний ключ. Содержимое файлов не выводится.

Новых пакетов на VPS нет. Настройки Qwen и данные сохраняются. Перезапускается
только Matchlab. Nginx, Docker и службы других проектов не перенастраиваются.
Импорт требует loopback и отдельный ключ. Публичный порт не открывается;
домен и сертификат не нужны.

## 2. Код и SSH-доступ на сервере Qwen

В терминале `user@user`, где показаны 62 ГиБ RAM:

```bash
git clone https://github.com/nutakoe27-commits/finding.git matchlab-scraper-src
cd matchlab-scraper-src
bash scripts/install_server_scraper.sh --dry-run
ssh root@VPS_IP true
```

Если каталог уже есть, используйте его и `git pull --ff-only origin main`.
Замените `VPS_IP` на действительный адрес VPS. При первом SSH-подключении
сверьте host key. Для нестандартного порта/ключа задайте SSH alias в
`~/.ssh/config` и используйте его вместо `root@VPS_IP`.

Фоновый туннель требует SSH-ключ без интерактивного запроса и без ssh-agent.
Если использовали только пароль, создайте отдельный ключ для сборщика
на сервере Qwen. Команда сохраняет уже существующий файл:

```bash
install -d -m 700 "$HOME/.ssh"
test -e "$HOME/.ssh/matchlab_scraper" || ssh-keygen -t ed25519 -f "$HOME/.ssh/matchlab_scraper" -N ''
ssh-copy-id -i "$HOME/.ssh/matchlab_scraper.pub" root@VPS_IP
ssh -i "$HOME/.ssh/matchlab_scraper" -o IdentitiesOnly=yes -o BatchMode=yes -o IdentityAgent=none -o StrictHostKeyChecking=yes root@VPS_IP true
```

`ssh-copy-id` один раз запросит пароль root на VPS и добавит только публичный
ключ в `authorized_keys`. Последняя проверка должна завершиться без запроса
пароля и без ошибок. Существующие ключи и SSH-конфигурация не заменяются.
Если выбранный ключ уже есть и защищён паролем, используйте другое свободное
имя для отдельного ключа фоновой службы. Скрипты сами ключи не меняют.

## 3. Туннель и сборщик на сервере Qwen

Из того же каталога на **сервере Qwen**:

```bash
sudo bash scripts/install_scraper_tunnel.sh --ssh-target root@VPS_IP --identity-file "$HOME/.ssh/matchlab_scraper"
sudo bash scripts/install_server_scraper.sh --api-url http://127.0.0.1:18081 --sources winline
```

Первый скрипт использует SSH-настройки вызывающего `sudo` пользователя
(`user`), копирует только ключ приёмника по SSH и создаёт отдельную службу
`matchlab-scraper-tunnel`. Для другого локального пользователя есть
`--local-user user`. Занятый порт 18081 вызывает остановку установки;
существующий процесс не меняется. Прежний туннель Qwen/порт 23333 сохраняется.
`--identity-file` выбирает один и тот же ключ для проверки, копирования файла
и фоновой службы. При уже настроенном SSH alias этот параметр необязателен.

Второй скрипт проверяет **MemAvailable ≥ 1536 МиБ** до изменений, создаёт
пользователя `matchlab-scraper`, код и venv в `/opt/matchlab-scraper`, состояние
в `/var/lib/matchlab-scraper`, скачивает Chromium. Нужны Python 3.10+ с venv,
доступ к PyPI и загрузке браузера. Зависимости модели не устанавливаются.

Сначала `matchlab-scraper-check` собирает данные настоящим браузером
30 секунд. Все **выбранные** источники должны отдать распознанные футбольные
котировки через активные потоки. Для `--sources winline` нужен только Winline.
Проверка с загрузкой страниц занимает до трёх минут.
Только после успеха включается `matchlab-scraper`. Код 2 означает неполную
проверку; работающий автосбор не заявляется.

Лимиты службы: 768 МиБ RAM, 50% одного CPU, низкий приоритет. Она не использует
GPU, не получает `.env` Qwen и доступ к `/root`. Ключ приёмника не наследуется
Chromium. Картинки, видео и шрифты не загружаются. При аварии сборщик
перезапускается с паузой, каждые шесть часов сессия планово обновляется.

Автоустановщик не запускает apt и не обновляет ОС. Если журнал сообщает
о недостающих системных библиотеках Chromium, установите их **на сервере Qwen**
и повторите второй скрипт:

```bash
sudo env NEEDRESTART_MODE=l /opt/matchlab-scraper/.venv/bin/python -m playwright install-deps chromium
```

## Проверка и управление

На сервере Qwen:

```bash
sudo systemctl status matchlab-scraper matchlab-scraper-tunnel --no-pager
sudo journalctl -u matchlab-scraper -n 40 --no-pager
```

На VPS:

```bash
curl -fsS http://127.0.0.1:8080/api/bookmakers | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["status"]["worker"],ensure_ascii=False,indent=2))'
```

В интерфейсе видны состояние автосбора, каждый источник и время полного успеха.
Список читает базу каждые 30 секунд; сборщик публикует раз в 60 секунд.
Нормальный результат: `state=ok`, `heartbeat_recent=true`, время котировок
увеличивается. При выборе одного Winline его успешное обновление даёт `ok`.
В дополнительном режиме `--sources betboom,winline` один доступный источник
даёт `partial`. Запущенный процесс systemd сам по себе не доказывает
поступление свежих данных.

При `blocked` сайт ограничил доступ, при `disconnected` потерян поток,
при `empty` нет распознанных матчей. Старые коэффициенты не стираются и не
получают новое время. После нескольких пропущенных интервалов интерфейс
показывает отсутствие связи. Ограниченные повторы открытия публичной страницы
не обходят защиту сайта. После исправления причины повторите проверку:

```bash
sudo systemctl reset-failed matchlab-scraper-check.service
sudo systemctl start matchlab-scraper-check.service
```

После её успеха: `sudo systemctl enable --now matchlab-scraper.service`.
Детали сбоя: `sudo journalctl -u matchlab-scraper-check -n 40 --no-pager`.

Если поток подключается, но состояние Winline — `empty`, нужен снимок
фактической структуры футбольной страницы. На сервере Qwen после обновления
репозитория можно выполнить диагностику без переустановки служб:

```bash
sudo install -m 644 scripts/diagnose_winline.py /opt/matchlab-scraper/diagnose_winline.py
sudo -u matchlab-scraper env PLAYWRIGHT_BROWSERS_PATH=/var/lib/matchlab-scraper/browsers \
  /opt/matchlab-scraper/.venv/bin/python /opt/matchlab-scraper/diagnose_winline.py \
  --output /var/lib/matchlab-scraper/winline-diagnostic.json
```

Диагностика ждёт загрузки публичной страницы 30 секунд, сохраняет видимый
текст, ссылки и контекст карточек в закрытый JSON, выводит краткий отчёт.
Она не подключается к приёмнику, не получает его ключ и не меняет данные
Matchlab. Формы, скрипты и значения параметров URL в снимок не включаются.
При повторе выберите другое имя выходного файла. Основная служба при этом
остаётся выключенной до успешной проверки реального разбора.

На отдельной странице Winline вид спорта подтверждается футбольным разделом
и заголовком «Футбол». Ранее сохранённая идентичность встречи может уточнить
турнир, но для получения новых футбольных карточек она не нужна. Без
подтверждённого турнира сохраняется «Турнир не указан», и прогноз АПЛ для
такой карточки недоступен. Принадлежность к АПЛ по названиям клубов не угадывается.
Для прежних захватов смешанной главной сохраняется строгая сверка по обеим
командам и времени; три коэффициента сами по себе не доказывают футбол.

Остановка только новых служб на сервере Qwen:

```bash
sudo systemctl disable --now matchlab-scraper.service matchlab-scraper-tunnel.service
```

Обновление: `git pull --ff-only origin main`,
`sudo systemctl stop matchlab-scraper`, повтор установки сборщика. Туннель
можно оставить работающим. История на VPS накапливается; при долгой работе
следите за свободным диском VPS.

## Проверено в разработке

Реальный публичный захват BetBoom/Winline, нормализация, локальная передача
с ключом, отказ при нехватке RAM, неполный/пустой результат, сохранение истории
при сбое. Браузер проверяется на локальном тестовом сайте. Доступ с IP сервера
Qwen и SSH к VPS проверяются установочными командами на ваших серверах.
Независимый разбор футбольной страницы проверен синтетическим примером
структуры карточки; актуальная структура этого раздела Winline проверяется
настоящим браузером при установке. Ранее пользовательский журнал подтвердил
получение семи событий со смешанной главной.
