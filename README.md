# Music Verifier

Music Verifier обрабатывает аудио и видео из YouGile, распознаёт музыку через ACRCloud, собирает результаты в отчёт и при включённой автоматизации ищет найденные произведения в CIS-Net. Результаты отправляются обратно в чат YouGile.

## Как устроена система

- `receiver` принимает вебхуки YouGile или опрашивает выбранные чаты, загружает файлы, извлекает аудио через FFmpeg и ставит задания в очередь. Он же отправляет уведомления и отчёты в YouGile.
- `worker` распознаёт аудио через ACRCloud и формирует сводный отчёт.
- `cisnet-browser` запускает Chromium; `cisnet-runner` выполняет поиск в CIS-Net по результатам отчёта.
- `init-data` создаёт каталог данных и базу SQLite при первом запуске. Очередь, результаты и файлы хранятся в `./data`; профиль браузера — в томе Docker `cisnet-profile`.

Исходный код приложения находится в `src/`, код контейнеров — в `docker/` и `cisnet-playwright/`, конфигурация запуска — в `compose.yaml` и `.env`.

## Установка и запуск

Нужны Git, Docker Engine и плагин Docker Compose.

Установка: [Docker Engine](https://docs.docker.com/engine/install/) и [плагин Docker Compose](https://docs.docker.com/compose/install/linux/).

1. Склонируйте проект с GitHub и перейдите в его каталог:

   ```bash
   git clone git@github.com:messazoid/yougile_auto.git
   cd yougile_auto
   ```

2. Создайте файл конфигурации из шаблона:

   ```bash
   sudo install -m 600 .env /etc/music-verifier.env
   sudoedit /etc/music-verifier.env
   ```

3. Заполните `WEBHOOK_SECRET`, `YOUGILE_API_KEY` и нужные `YOUGILE_ALLOWED_*_IDS`. Для подписки на вебхуки нужен `YOUGILE_ALLOWED_COLUMN_IDS`. Если планируете распознавание и поиск, укажите также `ACR_ACCESS_KEY`, `ACR_SECRET_KEY`, `CISNET_EMAIL` и `CISNET_PASSWORD`. При первом запуске оставьте `ACR_EXECUTE=0`, `CISNET_EXECUTE=0` и `YOUGILE_POLL_ENABLED=false`.

4. Соберите Docker-образы и запустите контейнеры:

   ```bash
   docker compose --env-file /etc/music-verifier.env config --quiet
   docker compose --env-file /etc/music-verifier.env build
   docker compose --env-file /etc/music-verifier.env up -d
   docker compose --env-file /etc/music-verifier.env ps -a
   curl --fail http://127.0.0.1:8080/health
   ```

При первом запуске создаётся новая база данных. HTTP-приёмник доступен на `127.0.0.1:8080`, интерфейс браузера noVNC — на `127.0.0.1:6080/vnc.html`.

## HTTPS через Caddy

Домен `music.example.com` должен указывать на сервер. Откройте входящие TCP-порты 80 и 443 в сетевом экране сервера и, если он есть, в правилах сети хостинга. Для UFW:

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
```

Порты 8080 и 6080 открывать извне не нужно: Docker привязывает их только к `127.0.0.1`. На Debian/Ubuntu установите Caddy из [официального репозитория](https://caddyserver.com/docs/install):

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install caddy
```

В `/etc/caddy/Caddyfile` укажите свой домен:

```caddyfile
music.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

Примените конфигурацию и проверьте HTTPS:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
curl --fail https://music.example.com/health
```

Caddy автоматически получает и обновляет TLS-сертификат. Чтобы создать подписки YouGile на вебхуки, выполните из каталога проекта, заменив домен на свой:

```bash
docker compose --env-file /etc/music-verifier.env exec receiver \
  python -m yougile_webhooks --public-base-url https://music.example.com --apply
```

После проверки HTTPS и создания подписок включите нужные функции в `/etc/music-verifier.env`: `ACR_EXECUTE=1` для распознавания и `CISNET_EXECUTE=1` для поиска. Опрос YouGile необязателен: при работе через вебхуки оставьте `YOUGILE_POLL_ENABLED=false`; если нужен дополнительный опрос, установите `YOUGILE_POLL_ENABLED=true`. Примените изменения:

```bash
docker compose --env-file /etc/music-verifier.env up -d
```

## Команды управления

Из каталога проекта установите команды в `/usr/local/bin`:

```bash
sudo ./bin/install-compose-commands
```

Команды используют `/etc/music-verifier.env` и текущий Compose-проект.

| Команда | Назначение и флаги |
| --- | --- |
| `yougile-start` | Запустить сервисы. |
| `yougile-stop` | Остановить `receiver`, `worker` и `cisnet-runner`; браузер остаётся запущенным. |
| `yougile-restart` | Пересоздать `receiver`, `worker` и `cisnet-runner`, применив изменения конфигурации. |
| `yougile-status [--full]` | Показать состояние контейнеров; `--full` дополнительно показывает процессы. |
| `yougile-logs [--once\|--follow] [--all]` | Показать логи с маскированием чувствительных полей. По умолчанию следит за новыми строками; `--once` выводит текущие, `--follow` включает слежение, `--all` добавляет служебные строки. |
| `yougile-httplogs [--once\|--follow] [--all]` | Показать HTTP-события обмена с YouGile; `--once` и `--follow` управляют слежением, `--all` принимается, но сохраняет фильтр HTTP-событий. |
| `yougile-cisnetlogs [--once\|--follow]` | Показать логи браузера и поиска CIS-Net; по умолчанию следит за новыми строками. |
| `yougile-runs` | Открыть список запусков; подкоманды приведены ниже. `--db ПУТЬ` выбирает базу, `--company-id ID` добавляет ссылки на чаты задач. |
| `yougile-resolve ССЫЛКА...` | Определить задачу, чат и колонку по ссылкам YouGile. `--json` выводит JSON, `--columns-env` — строку с ID колонок, `--columns` открывает интерактивный выбор колонок и сохраняет их в файл конфигурации. |
| `yougile-cisnet` | Запустить интерактивный поиск; подкоманды и флаги приведены ниже. |
| `recognize-wav [-i\|--input] ФАЙЛ.wav [--force]` | Поставить локальный WAV в очередь; `--force` создаёт новый запуск даже для уже обработанного файла. |
| `yougile-env` | Открыть `/etc/music-verifier.env` для редактирования. |
| `yougile-reset-data` | Показать план очистки данных. `--execute 'RESET MUSIC-VERIFIER DATA'` выполняет очистку и оставляет сервисы остановленными. |

Для `yougile-runs` идентификатор запуска записывается как `S14`:

| Подкоманда | Назначение и флаги |
| --- | --- |
| `list`, `show S14` | Список запусков или подробности одного запуска. |
| `view S14 scan\|responses\|aggregation [--limit N] [--raw]` | Просмотр результата этапа; `--limit` ограничивает число строк, `--raw` выводит исходный JSON ответов или агрегации. |
| `continue S14 --confirm 'CONTINUE S14'` | Продолжить поддерживаемый незавершённый этап. |
| `replay S14 --confirm 'REPLAY S14'` | Поставить повторную обработку в очередь. |
| `trash-audio\|trash-results\|trash-all S14 --confirm 'TRASH S14'` | Переместить выбранные файлы запуска в `data/.trash`. |

Для `yougile-cisnet` доступны два режима:

| Подкоманда | Назначение и флаги |
| --- | --- |
| `run ИМЯ_ЗАПУСКА --candidates 1,2\|all` | Поиск по кандидатам из отчёта. |
| `manual --title НАЗВАНИЕ --performer ИСПОЛНИТЕЛЬ [--iswc КОД]` | Поиск по заданным данным; `--iswc` переключает поиск на код ISWC. |

Оба режима без `--execute` (`--exe`) показывают план поиска; этот флаг запускает запросы в CIS-Net. `--overwrite-results` повторно записывает результат, сохранив предыдущий файл.
