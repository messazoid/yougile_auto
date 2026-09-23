# Восстановление без `/opt/yougile-video`

Инструкция восстанавливает текущий `/opt/music-verifier` из проверенной копии
`/var/backups/music-verifier/20260827T153949Z/`. Старый каталог не требуется.
Git и внешние загрузки пакетов не используются.

## Перед восстановлением

1. Остановить `yougile-receiver.service` и убедиться, что receiver и FFmpeg
   завершены.
2. Если `/opt/music-verifier` существует, сохранить его `data/` в новый
   root-only каталог. Новые события нельзя перезаписывать данными из копии.
3. Проверить не менее 2 ГБ свободного места, пользователя и группу `yougile`,
   системный Python 3.12 и FFmpeg.

Пример сохранения новых данных:

```bash
recovery_capture=/var/backups/music-verifier/$(date -u +%Y%m%dT%H%M%SZ)-before-restore
install -d -o root -g root -m 0700 "$recovery_capture/data"
rsync -aHAX --numeric-ids /opt/music-verifier/data/ "$recovery_capture/data/"
```

Если существующее дерево повреждено, не удалять его: после остановки службы
переименовать в отдельный точный путь и восстановить чистое дерево рядом.

## Восстановление файлов

```bash
recovery_source=/var/backups/music-verifier/20260827T153949Z/project
install -d -o root -g yougile -m 0750 /opt/music-verifier
rsync -aHAX --numeric-ids "$recovery_source/" /opt/music-verifier/
install -d -o root -g yougile -m 0750 /opt/music-verifier/venvs
```

Копия содержит код, wheel-файлы, конфигурацию, документацию, тесты, архив,
эталон, исторические результаты и устойчиво скопированные рабочие данные. Venv
намеренно восстанавливаются заново.

## Воссоздание окружений без сети

```bash
python3 -m venv /opt/music-verifier/venvs/receiver
/opt/music-verifier/venvs/receiver/bin/python -m pip install \
  --no-index --find-links /opt/music-verifier/config/packages/receiver pip==26.2.1
/opt/music-verifier/venvs/receiver/bin/python -m pip install \
  --no-index --find-links /opt/music-verifier/config/packages/receiver \
  -r /opt/music-verifier/config/receiver-requirements.lock

python3 -m venv /opt/music-verifier/venvs/acr-sdk
/opt/music-verifier/venvs/acr-sdk/bin/python -m pip install \
  --no-index --find-links /opt/music-verifier/config/packages/acr-sdk pip==24.0
/opt/music-verifier/venvs/acr-sdk/bin/python -m pip install \
  --no-index --no-deps --find-links /opt/music-verifier/config/packages/acr-sdk \
  pyacrcloud==1.0.12

chown -R root:yougile /opt/music-verifier/venvs
chmod -R go-w /opt/music-verifier/venvs
find /opt/music-verifier/venvs -type d -exec chmod o-rwx {} +
chown root:yougile /opt/music-verifier/src/receiver.py /opt/music-verifier/src/scan.py
chmod 0640 /opt/music-verifier/src/receiver.py /opt/music-verifier/src/scan.py
```

После копирования проверить числовые uid/gid данных. На сервере `test` рабочие
`data/audio`, `data/incoming`, `data/events` принадлежат `yougile:yougile` и
имеют режим `0750`; приватные `data/runs` и диагностика остаются root-only.

## Проверка до запуска

```bash
cd /opt/music-verifier
sha256sum --quiet -c docs/SHA256SUMS
/opt/music-verifier/venvs/receiver/bin/python -m pip check
/opt/music-verifier/venvs/acr-sdk/bin/python -m pip check
/opt/music-verifier/venvs/acr-sdk/bin/python -m unittest discover -s tests -v
systemd-analyze verify /opt/music-verifier/deploy/yougile-receiver.service
```

Проверить SQLite через read-only `PRAGMA integrity_check`, нативную библиотеку
ACRCloud по SHA256
`e8638e0a29455b3ac8098b92a8e51a69e8928fce69e335d18155d4ce0490948a`
и отсутствие `src/data`.

## Системные файлы и запуск

Текущий unit и защищённая копия env находятся в `system/` резервной копии.
Существующий `/etc/yougile-video.env` не перезаписывать без отдельного сравнения.

```bash
install -o root -g root -m 0644 \
  /var/backups/music-verifier/20260827T153949Z/system/yougile-receiver.service \
  /etc/systemd/system/yougile-receiver.service
systemctl daemon-reload
systemctl start yougile-receiver.service
systemctl status yougile-receiver.service
```

После запуска проверить один процесс, `WorkingDirectory=/opt/music-verifier`,
команду с `receiver:app --app-dir /opt/music-verifier/src`, порт
`127.0.0.1:8080` и локальный `/health`.

## Статус локальной проверки восстановления

Копия `20260827T153949Z` восстановлена в отдельный временный каталог без
использования `/opt/yougile-video`. Проверены:

- совпадение статических файлов по `docs/SHA256SUMS`;
- создание обоих venv только из локального wheelhouse и lock-файлов;
- точное совпадение `pip freeze --all`, успешный `pip check` и SHA256 нативной
  библиотеки ACRCloud;
- 27 исходных тестов при системной блокировке сетевых соединений и запуска
  дочерних процессов;
- безопасный импорт receiver с данными в корне восстановленной копии, без
  создания `src/data`;
- `PRAGMA integrity_check` для двух SQLite и проверка шаблона unit.

Не проверялись установка unit в `/etc`, запуск восстановленной службы,
пригодность действующих секретов, восстановление Caddy и внешние цепочки
YouGile, Яндекс.Диск и ACRCloud. Второй receiver не запускался.
