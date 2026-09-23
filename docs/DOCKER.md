# Docker Compose: сборка и администрирование

Комплект предназначен для нового сервера или staging. Он не переключает и не
останавливает текущие systemd-сервисы.

## Подготовка

Нужны Docker Engine с Compose v2 и x86_64/glibc host. Репозитории должны лежать
рядом:

```text
/opt/music-verifier
/opt/cisnet-playwright
```

Если путь второго репозитория другой, измените только
`CISNET_BUILD_CONTEXT` в рабочем EnvironmentFile.

Корневой `.env` — отслеживаемый безопасный шаблон. Не редактируйте его на
сервере. Создайте защищённый рабочий EnvironmentFile и заполните пустые поля:

- `WEBHOOK_SECRET`, `YOUGILE_API_KEY`;
- `ACR_ACCESS_KEY`, `ACR_SECRET_KEY` и затем `ACR_EXECUTE=1`;
- `CISNET_EMAIL`, `CISNET_PASSWORD`;
- `VNC_PASSWORD`.

```bash
sudo install -o root -g root -m 600 \
  /opt/music-verifier/.env /etc/music-verifier.env
sudoedit /etc/music-verifier.env
sudo docker compose --project-directory /opt/music-verifier \
  --env-file /etc/music-verifier.env config --quiet
```

Не используйте обычный `docker compose config` в отчётах: после заполнения он
может вывести секреты. `/etc/music-verifier.env` нельзя коммитить или помещать
в image.

## Сборка и первый запуск

```bash
sudo docker compose --project-directory /opt/music-verifier \
  --env-file /etc/music-verifier.env build --pull
sudo docker compose --project-directory /opt/music-verifier \
  --env-file /etc/music-verifier.env up -d
sudo docker compose --project-directory /opt/music-verifier \
  --env-file /etc/music-verifier.env ps
```

`init-data` первым создаёт named volume `music-verifier_music-data`. Первый
запуск разрешён только для пустого volume. В нём создаются пустые каталоги и
`queue/pipeline.sqlite3` со schema 12, CIS-Net baseline и нулём operational
records. Старые WAV, результаты, сообщения и прогоны туда не копируются.
Существующий host-каталог `/opt/music-verifier/data` в Compose не монтируется и
этой процедурой не изменяется.

Повторный запуск initializer не очищает существующие данные. Не используйте
`docker compose down -v`: этот ключ удаляет persistent volumes.

## Проверка

```bash
curl --fail http://127.0.0.1:8080/health
sudo docker compose --project-directory /opt/music-verifier \
  --env-file /etc/music-verifier.env ps
sudo docker compose --project-directory /opt/music-verifier \
  --env-file /etc/music-verifier.env \
  logs --tail=100 receiver worker cisnet-runner cisnet-browser
sudo docker compose --project-directory /opt/music-verifier \
  --env-file /etc/music-verifier.env \
  exec -T receiver python docker/healthcheck.py database
```

noVNC доступен по `http://127.0.0.1:6080/vnc.html`. Для удалённого доступа
используйте SSH tunnel. Порты VNC `5900` и CDP `9223` на host не публикуются.

Healthchecks проверяют receiver HTTP, SQLite, heartbeat CIS-Net runner, CDP и
noVNC. Они не выполняют live-запросы в YouGile, ACRCloud или CIS-Net.

## Операторские команды в Compose

После клонирования установите Compose-native wrappers одной командой:

```bash
sudo /opt/music-verifier/bin/install-compose-commands
```

После этого `yougile-start`, `yougile-stop`, `yougile-restart`, `yougile-status`,
`yougile-logs`, `yougile-httplogs`, `yougile-cisnetlogs`, `yougile-runs`,
`yougile-resolve`, `yougile-cisnet`, `yougile-env`, `yougile-reset-data` и
`recognize-wav` доступны из любой рабочей директории. Они используют
`/etc/music-verifier.env` и `/opt/music-verifier` автоматически.

Основные команды:

```bash
yougile-start
yougile-stop
yougile-restart
yougile-status
yougile-logs --follow
yougile-cisnetlogs --follow
```

Остальные созданные инструменты находятся в image и запускаются так:

```bash
# Интерактивный список и просмотр прогонов
yougile-runs

# Ручной WAV: файл копируется во временный tmpfs worker-контейнера
recognize-wav /absolute/path/input.wav

# Ручной CIS-Net поиск; session lock защищает от параллельного runner
yougile-cisnet manual \
  --title 'Название' --performer 'Исполнитель' --execute
```

`yougile-env` открывает `/etc/music-verifier.env`. `yougile-reset-data` требует
точное подтверждение `RESET MUSIC-VERIFIER DATA` и удаляет persistent volume;
не запускайте её в штатной эксплуатации. Команды `yougile_resolve.py`,
`yougile_webhooks.py`, baseline и backfill остаются специальными
live/migration-инструментами: перед их запуском требуется отдельная проверка
scope и явное разрешение на изменение внешнего состояния.

## Обновление

```bash
git pull --ff-only
sudo docker compose --project-directory /opt/music-verifier \
  --env-file /etc/music-verifier.env build --pull
sudo docker compose --project-directory /opt/music-verifier \
  --env-file /etc/music-verifier.env up -d --remove-orphans
yougile-status
```

Не запускайте Compose одновременно со старым systemd deployment: два receiver
или worker могут повторно обработать события и потратить ACRCloud requests.

## Данные и backup

`music-data` содержит SQLite, WAL/SHM, WAV, recognition, aggregation и CIS-Net
artifacts. `cisnet-profile` содержит browser profile и служебные настройки
desktop-home. Остановка или пересоздание контейнеров volumes не удаляет.

Для согласованного backup сначала требуется отдельное окно остановки всех
writers. Архивируйте весь `music-data`, а не один `pipeline.sqlite3`; секретный
`/etc/music-verifier.env` храните отдельным защищённым архивом. Перед
восстановлением проверяйте SHA-256, ownership, `PRAGMA quick_check` и foreign
keys.

## Ограничения текущей проверки

На исходном сервере Docker Engine отсутствует. Поэтому Dockerfile/Compose
проверены статически, Python/Node/shell tests выполняются на host, но фактическая
сборка images и staging smoke должны быть выполнены на Docker host до cutover.
