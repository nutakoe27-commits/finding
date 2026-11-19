# Фотографии Миры

Пользователь просит фото кнопкой камеры возле поля ввода или сообщением «Пришли фото». Он выбирает сюжет и может продолжить разговор, пока готовится снимок. Для сюжетов «В белье» и «Ню» требуется явное подтверждение. В истории такие фото открываются по нажатию. Очистка переписки удаляет и готовые фото, и ожидающие запросы.

На сайте доступ к изображению проверяется по текущему аккаунту. На GPU отправляются только идентификаторы задания и название выбранного сюжета. GPU-сервер забирает задания с сайта через исходящий HTTPS по отдельному ключу. Один экземпляр обработчика обслуживает одну V100 и генерирует один снимок за раз.

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

Для селфи используется аватар Миры как исходное изображение. Первый проход строит портрет 512×640, второй уточняет детали при 640×800. Используются общие веса SD 1.5 в FP16. Ограничение аллокатора PyTorch — четверть памяти V100. Задания выполняются при активном чате. Для остановки генерации и освобождения памяти выполните `systemctl --user disable --now mira-photo.service`.
