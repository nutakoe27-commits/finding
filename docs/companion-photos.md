# Фотографии Миры

Пользователь просит фото кнопкой камеры возле поля ввода или сообщением «Пришли фото». Он выбирает сюжет и может продолжить разговор, пока готовится снимок. Для сюжетов «В белье» и «Ню» требуется явное подтверждение. В истории такие фото открываются по нажатию. Очистка переписки удаляет и готовые фото, и ожидающие запросы.

На сайте доступ к изображению проверяется по текущему аккаунту. На GPU отправляются только идентификаторы задания и название выбранного сюжета. GPU-сервер забирает задания с сайта через исходящий HTTPS по отдельному ключу. Один экземпляр обработчика обслуживает одну V100 и генерирует один снимок за раз.

## Обновление работающего генератора до RealVisXL

На VPS сайта обновите ветку и подготовьте архив. Обновление генератора можно провести при работающем сайте.

```bash
cd /root/matchlab &&
git pull --ff-only origin feat/companion-chat &&
PYTHONPATH=. .venv/bin/python scripts/package_companion_photo.py
```

На GPU-сервере выполните команды под владельцем службы. Новым весам и адаптеру требуется около 10 ГБ диска в кеше Hugging Face. Во время скачивания прежний генератор продолжает работать. Для пробного портрета служба фотографий кратковременно останавливается. Если пробный запуск завершается ошибкой, скрипт возвращает прежний обработчик.

```bash
cd ~/mira-photo &&
umask 077 &&
stage=$(mktemp -d "$PWD/realvis.XXXXXXXX") &&
scp root@89.111.143.45:/root/matchlab/.local/mira-photo-worker.tar.gz "$stage/package.tar.gz" &&
tar -xzf "$stage/package.tar.gz" -C "$stage" worker.py mira.webp upgrade-xl.sh &&
bash "$stage/upgrade-xl.sh"
```

После проверки откройте ~/mira-photo/realvis-cafe.jpg или запросите фото в чате. В журнале службы должно появиться `Photo worker ready (realvisxl)`. Для возврата к SD 1.5 выполните `systemctl --user edit mira-photo.service`, добавьте строки `[Service]` и `Environment=MIRA_PHOTO_MODEL=sd15`, затем выполните `systemctl --user restart mira-photo.service`.

## Установка на VPS сайта

Выполните из аккаунта, из которого установлен Matchlab.

```bash
cd /root/matchlab &&
git switch feat/companion-chat &&
git pull --ff-only origin feat/companion-chat &&
PYTHONPATH=. .venv/bin/python scripts/package_companion_photo.py &&
systemctl restart matchlab.service
```

Команда выведет путь к закрытому архиву. Внутри ключ доступа обработчика. Передавайте архив по SSH. При первом запуске сайт создаст таблицу фотографий.

## Установка на GPU-сервере

Используйте аккаунт, под которым прошёл тест 512×512. Обработчику нужны установленные в `~/mira-photo/.venv` PyTorch 2.5.1 CUDA 11.8, torchvision 0.20.1, diffusers 0.32.2, transformers 4.48.3, accelerate 1.3.0, huggingface-hub < 1, safetensors и requests. Если путь к архиву из первого шага отличается, подставьте фактический путь в scp.

```bash
cd ~/mira-photo &&
umask 077 &&
scp root@89.111.143.45:/root/matchlab/.local/mira-photo-worker.tar.gz . &&
tar -xzf mira-photo-worker.tar.gz worker.py photo-worker.key mira-photo.service mira.webp &&
install -Dm644 mira-photo.service ~/.config/systemd/user/mira-photo.service &&
sudo loginctl enable-linger "$USER" &&
systemctl --user daemon-reload &&
systemctl --user enable mira-photo.service &&
systemctl --user restart mira-photo.service
```

По умолчанию обработчик обращается к https://prognozich.online. Если сайт перенесён, задайте переменную MIRA_SITE в конфигурации службы. При первой загрузке модель берётся из кеша Hugging Face пользователя или скачивается с закреплённой ревизии.

## Проверка

```bash
journalctl --user -u mira-photo.service -n 20 -f -o cat
```

После `Photo worker ready` откройте чат, попросите селфи и отправьте обычное сообщение во время генерации. Обработчик пишет время генерации и пик памяти аллокатора PyTorch. Проверка реального остатка свободной памяти и нагрузки карты:

```bash
nvidia-smi -i GPU-5cc54044-df38-0b75-15d6-a9e419fd2b5e --query-gpu=timestamp,memory.free,utilization.gpu --format=csv -l 1
```

RealVisXL V5.0 генерирует портреты 768×960. IP-Adapter Plus Face получает аватар Миры для каждого из четырёх сюжетов. Модель использует FP16 и переносит компоненты между RAM и GPU по мере необходимости. Под веса предусмотрите около 10 ГБ свободного места в кеше Hugging Face. Аллокатор PyTorch ограничен четвертью памяти второй V100. Для остановки генерации и освобождения памяти выполните `systemctl --user disable --now mira-photo.service`.

[RealVisXL V5.0](https://huggingface.co/SG161222/RealVisXL_V5.0) · [IP-Adapter](https://huggingface.co/h94/IP-Adapter)
