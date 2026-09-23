# music-verifier

`music-verifier` — серверный конвейер, который получает музыкальные материалы
из YouGile, готовит WAV, выполняет распознавание ACRCloud, агрегирует кандидатов,
проверяет их через CIS-Net MWI и ставит итоговые сообщения в надёжную очередь
публикации обратно в YouGile.

```text
YouGile
  -> receiver: webhook или резервный poller
  -> загрузка YouGile/Яндекс.Диск
  -> FFmpeg/FFprobe -> WAV
  -> последовательный ACRCloud worker
  -> aggregation-v2-calibrated
  -> CIS-Net Playwright
  -> durable outbox
  -> сообщение в чат YouGile
```

Основной проект находится в `/opt/music-verifier`. Видимый Chromium, Xvfb,
VNC/noVNC и Node/Playwright находятся в связанном проекте
`/opt/cisnet-playwright`. Для рабочей цепочки нужны оба проекта.

## Критические запреты

> - Не удаляйте, не заменяйте и не пересоздавайте production-базу
>   `data/queue/pipeline.sqlite3`.
> - Не запускайте одновременно два receiver, worker, poller или CIS-Net runner
>   на одном persistent state.
> - Не повторяйте ACRCloud-запрос без установленной причины: кандидаты и каждая
>   попытка уже сохраняются в SQLite.
> - Не считайте `complete_candidates` подтверждённым произведением.
> - Не публикуйте `.env`, API-ключи, пароли, cookies, заголовки, тела запросов и
>   временные URL в Git, image, Compose YAML, лог или отчёт.
> - Не выполняйте live login/search/upload/Create/download в CIS-Net без явного
>   разрешения. Сотрудник имеет приоритет; при занятой сессии допустима только
>   точная кнопка `No`. `Yes` допустима только для подтверждения Logout.

## Что реализовано

Реализовано в production-коде:

- FastAPI receiver для YouGile webhook и опционального poller;
- durable deduplication событий, сообщений, источников и распознаваний;
- загрузка разрешённых YouGile-файлов и публичных ссылок/папок Яндекс.Диска;
- сохранение всех разных поддерживаемых ссылок из одного сообщения без потери
  промежуточных источников;
- подготовка mono PCM WAV через FFmpeg с проверкой FFprobe;
- один последовательный ACRCloud worker с учётом попыток и окон;
- сохранение raw response, кандидатов и scan SQLite для каждого recognition;
- агрегация `aggregation-v2-calibrated` и export contract
  `aggregation-files/v3`;
- durable CIS-Net state, visible Chromium, безопасный busy-session path,
  поиск MWI и Logout;
- durable `chat_notifications`, которые receiver публикует в связанные чаты
  YouGile идемпотентно.

На 2026-09-23 текущий runtime подтверждал работу этих слоёв по persistent
state: схема приложения 12, две завершённые recognition/aggregation/CIS-Net
цепочки и отправленные outbox-записи. Это операционный снимок, а не гарантия
текущего состояния; перед действиями проверяйте `/health`, systemd и SQLite.
В рамках последнего аудита live-запросы в YouGile, ACRCloud и CIS-Net не
выполнялись.

Только статически или ограниченно подтверждено:

- Dockerfile, Compose, initializer и healthchecks реализованы, но Docker Engine
  на текущем host отсутствует и images ещё не собирались;
- полный suite из 221 теста прошёл в отдельном network namespace без внешней
  сети и production data; два native ACRCloud-теста дополнительно прошли в
  SDK-окружении;
- live-проверка CIS-Net после очистки не выполнялась; production adapter
  подтверждён статическими и unit-проверками.

Не завершено:

- не выполнены image build и staging smoke на Docker host;
- текущий host-systemd deployment ещё не переключён на контейнерный;
- worker healthcheck подтверждает process lifecycle и SQLite, но не измеряет
  прогресс длительного ACRCloud scan;
- исходники и история хранятся в GitHub-репозиториях `messazoid/yougile_auto`
  и `messazoid/cidnet_auto`;
- Compose deployment и cutover не прошли staging/live validation.

## Карта проекта

### Runtime-код

- `src/receiver.py` — FastAPI, webhook, poller, source acquisition, FFmpeg,
  receiver loop и публикация outbox;
- `src/job_store.py` — схема SQLite 12, migrations, leases, deduplication,
  request accounting и durable queues;
- `src/recognition_worker.py` — global worker lock, ACRCloud и запуск
  aggregation;
- `src/scan.py` — fingerprint-окна, ACRCloud transport, retry ledger и scan
  artifacts;
- `src/aggregation/` — production aggregation и атомарный export;
- `src/cisnet_automation.py`, `src/cisnet_store.py`, `src/cisnet_cli.py` —
  staging, browser runner, durable CIS-Net state и outbox;
- `scripts/cisnet_search_works.js` — production Playwright adapter;
- `yougile-cisnet` — текущая host-systemd обёртка для desktop/CDP/browser;
- `/opt/cisnet-playwright/launch-cdp.js` — фактический CDP launcher на `9223`;
- `/opt/cisnet-playwright/cisnet-desktop.sh` — Xvfb, Fluxbox, VNC и noVNC.

### Операторские инструменты

- `yougile-status`, `yougile-logs`, `yougile-httplogs`,
  `yougile-cisnetlogs` — состояние и privacy-safe логи;
- `yougile-runs` — просмотр run state и подтверждаемые операции continue,
  replay и trash;
- `recognize-wav` — постановка локального WAV в production queue;
- `src/yougile_resolve.py` — live-разрешение ссылок YouGile;
- `src/yougile_webhooks.py` — live-проверка/создание webhook subscriptions;
- `src/baseline_yougile.py`, `src/backfill_wav.py` — специальные mutable
  migration/backfill tools;
- `yougile-reset-data` — разрушительный аварийный инструмент, не штатная
  команда. В этом документе намеренно нет примера его запуска.

### Разработка и сопровождение

- `tests/` — unit/regression suite; сохраняется в source repository, но не
  должен попадать в production image;
- `offline/` — offline aggregation validation и reference material; не runtime;
- `config/*requirements*`, `/opt/cisnet-playwright/package-lock.json` —
  dependency locks;
- `config/packages/` — проверяемый SHA-256 offline wheelhouse для текущего
  Python 3.12/x86_64 build; не копировать в final image;
- `deploy/` — base systemd units текущего, не Compose deployment;
- `docs/RECOVERY.md` — disaster recovery, не обычная эксплуатация.

Backup-каталоги, `*.backup-*`, `tmp/`, `.playwright-mcp/`, `__pycache__/`,
`.pytest_cache/`, venv, `node_modules`, browser cache, screenshots и логи не
являются исходным комплектом.

## Persistent data и SQLite

Вся директория `data/` — persistent state, а не cache:

- `data/incoming/` — временно загруженные исходники;
- `data/audio/` — подготовленные WAV;
- `data/events/` — последняя диагностическая копия webhook;
- `data/queue/pipeline.sqlite3` — authoritative pipeline state;
- `data/recognition-runs/` — scan SQLite, responses, matches и manifests;
- `data/aggregation/` — public aggregation exports;
- `data/cisnet/` — CIS-Net requests/results и общий session lock;
- `data/.trash/` — появляется только после подтверждённого archive action.

На 2026-09-23 `data/` была непустой (около 39 MiB), база работала в WAL mode,
а рядом существовали `pipeline.sqlite3-wal` и `pipeline.sqlite3-shm`.
`schema_info.version=12`; `PRAGMA user_version` намеренно не является номером
прикладной схемы. Read-only проверки дали `quick_check=ok` и 0 нарушений
foreign keys.

Текущие права: каталоги верхнего уровня `data/` — `yougile:yougile`, mode 750;
БД, WAL/SHM и runtime-файлы — обычно mode 600; каталоги отдельных run — 700.
На новом сервере нужны те же принципы даже при других UID/GID.

SQLite размещайте только на локальном filesystem с корректными POSIX locks, не
на NFS/SMB. Receiver, worker и CIS-Net могут совместно использовать одну WAL
базу, если каждый сервис имеет один экземпляр и использует штатные transactions,
leases и lock files. Никогда не масштабируйте worker/poller горизонтально.

Для read-only диагностики открывайте URI `mode=ro`. `immutable=1` допустим
только для гарантированно остановленного и неизменяемого snapshot. Активную БД
нельзя копировать одним файлом: сначала нужен согласованный freeze, затем
snapshot всей `data/` с метаданными.

## Конфигурация и секреты

Текущий systemd читает:

- receiver: `/etc/yougile-video.env`, root:root, mode 600;
- worker: `/etc/music-verifier-worker.env`, root:root, mode 600;
- CIS-Net: `/opt/cisnet-playwright/config/.env`, root:root, mode 600;
- VNC password: `/var/lib/cisnet-playwright/.vnc/passwd`, cisbot:cisbot,
  mode 600.

В source tree также есть protected local copies
`config/yougile-video.env` и `config/music-verifier-worker.env`. Они не являются
образцами, не должны попадать в Git/image/build context и не заменяют
фактические `/etc` EnvironmentFile. Для шаблонов используются только
`*.env.example`.

Имена переменных, которые реально читает код или wrapper:

Receiver и YouGile:

- `WEBHOOK_SECRET`
- `YOUGILE_API_BASE`
- `YOUGILE_API_KEY`
- `YOUGILE_ALLOWED_BOARD_IDS`
- `YOUGILE_ALLOWED_COLUMN_IDS`
- `YOUGILE_ALLOWED_TASK_IDS`
- `YOUGILE_ALLOWED_CHAT_IDS`
- `YOUGILE_POLL_ENABLED`
- `YOUGILE_POLL_TASK_ID`
- `YOUGILE_POLL_CHAT_ID`
- `YOUGILE_POLL_INTERVAL_SECONDS`
- `YOUGILE_POLL_INITIAL_LIMIT`
- `YOUGILE_POLL_MAX_CHATS_PER_CYCLE`
- `YOUGILE_MAX_REQUESTS_PER_MINUTE`
- `YOUGILE_SCOPE_REFRESH_SECONDS`
- `YOUGILE_WEBHOOK_PUBLIC_BASE` (только webhook admin CLI)
- `YOUGILE_COMPANY_ID` (ссылки в `yougile-runs`)
- `MIN_FREE_DISK_MB`
- `MAX_VIDEO_MB`
- `YANDEX_DISK_MAX_VIDEO_MB`
- `FFMPEG_TIMEOUT_SECONDS`

ACRCloud worker:

- `ACR_HOST`
- `ACR_ACCESS_KEY`
- `ACR_SECRET_KEY`
- `ACR_EXECUTE`
- `ACR_MAX_RETRIES`
- `ACR_RETRY_BASE_SECONDS`
- `ACR_RETRY_MAX_SECONDS`

CIS-Net/Playwright:

- `CISNET_EMAIL`
- `CISNET_PASSWORD`
- `CISNET_CDP_ENDPOINT`
- `PLAYWRIGHT_BROWSERS_PATH`
- `DISPLAY`
- `HOME`

`CISNET_ENV_LOADED` — внутренний marker wrapper, не пользовательская настройка.
Resolver и receiver используют единое имя `YOUGILE_ALLOWED_COLUMN_IDS`.

Compose читает отслеживаемый корневой `.env`: несекретные значения сохранены,
а поля credentials пустые. На целевом сервере администратор заполняет локальную
копию и устанавливает mode 600. Заполненный файл нельзя коммитить; значения не
помещаются в Dockerfile, image или Compose YAML.

## Текущий systemd deployment

Это описание нынешнего сервера, а не будущего Compose.

| Unit | Назначение | User/Group | Фактический entrypoint |
|---|---|---|---|
| `yougile-receiver.service` | receiver, publisher, optional poller | `yougile:yougile` | receiver venv, Uvicorn, `127.0.0.1:8080` |
| `music-verifier-worker.service` | ACRCloud + aggregation | `yougile:yougile` | ACR SDK venv, `src/recognition_worker.py` |
| `music-verifier-cisnet.service` | one-shot CIS-Net automation | `root` | receiver venv, `src/cisnet_automation.py` |
| `music-verifier-cisnet.timer` | retry once per minute | systemd | triggers CIS-Net service |
| `music-verifier-cisnet.path` | watch `data/aggregation` | systemd | triggers CIS-Net service |
| `music-verifier.target` | receiver + worker | systemd | target |
| `cisnet-desktop.service` | Xvfb/Fluxbox/VNC/noVNC | `cisbot:cisbot` | `cisnet-desktop.sh` |
| `cisnet-cdp-browser.service` | transient CDP browser | `cisbot:cisbot` | создаётся `systemd-run`, постоянного unit нет |

На момент аудита receiver, worker, target, timer и path были active; CIS-Net
one-shot — inactive после успешного запуска; desktop — inactive/disabled;
постоянный CDP unit отсутствовал. Локальный `/health` возвращал `status=ok`,
нулевую очередь и `yougile_polling_enabled=false`.

Файлы `deploy/` совпадают с base unit files в `/etc/systemd/system`, но runtime
также зависит от отсутствующих в `deploy/` файлов:

- `/etc/systemd/system/music-verifier-worker.service.d/aggregation.conf`;
- `/etc/systemd/system/music-verifier-cisnet.service.d/override.conf`;
- `/etc/systemd/system/cisnet-desktop.service`.

Поэтому `deploy/` сам по себе не воспроизводит текущий systemd deployment.

Порты и bind policy:

- receiver — `127.0.0.1:8080`;
- Xvfb — display `:99`, TCP для X11 отключён;
- VNC — localhost `5900`;
- noVNC — `127.0.0.1:6080`;
- CDP — `127.0.0.1:9223`, только на время browser session.

На момент аудита слушал только `127.0.0.1:8080`. CDP, VNC и noVNC нельзя
публиковать в общую сеть; используйте localhost и SSH tunnel.

## Версии и зависимости текущего сервера

| Компонент | Версия/источник |
|---|---|
| Ubuntu | 24.04.5 LTS, x86_64 |
| Python | 3.12.3 |
| SQLite (Python) | 3.45.1 |
| receiver packages | `config/receiver-requirements.lock` |
| ACR SDK | `pyacrcloud==1.0.12`, `config/acr-sdk-requirements.lock` |
| FFmpeg/FFprobe | 6.1.1-3ubuntu5 |
| Node.js | 24.21.0 |
| npm | 11.19.0 |
| Playwright | 1.62.1, exact version in `package-lock.json` |
| Chromium | Chrome for Testing 151.0.7922.34 |
| Xvfb | 21.1.12 package line |
| x11vnc | 0.9.16 |
| noVNC/websockify | 1.3.0 / 0.10.0 |
| Fluxbox | 1.3.7 |

Также нужны `bash`, `curl`, `flock`, `runuser`/эквивалент контейнерного user
switch и системные библиотеки Chromium. Native `pyacrcloud` wheel содержит
x86_64 glibc `.so`; другой CPU/дистрибутив требует отдельной проверки.

Не копируйте `venvs/`, `node_modules/` или
`/var/lib/cisnet-playwright/.cache/ms-playwright`: установите зависимости из
lock-файлов и загрузите Chromium на build stage. Offline wheelhouse применим
только к совместимому Python 3.12/x86_64 build и должен пройти SHA-256 check.

## Консольные команды

### Безопасная диагностика

```bash
yougile-status
yougile-status --full
curl --fail --silent --show-error http://127.0.0.1:8080/health
yougile-logs --once
yougile-httplogs --once
yougile-cisnetlogs --once
systemctl show yougile-receiver.service music-verifier-worker.service \
  music-verifier-cisnet.service music-verifier-cisnet.timer \
  music-verifier-cisnet.path music-verifier.target cisnet-desktop.service \
  -p Id -p LoadState -p ActiveState -p SubState -p MainPID -p Result
```

Не публикуйте raw journal output. `yougile-logs` фильтрует известные webhook и
Bearer fragments, но оператор всё равно обязан просмотреть вывод перед
передачей третьим лицам.

Логически read-only режимы run console:

```bash
yougile-runs list
yougile-runs show S1
yougile-runs view S1 scan --limit 20
yougile-runs view S1 aggregation --limit 20
```

Не используйте `--raw` в общем терминале: вывод может содержать пользовательские
metadata. Текущая реализация `list/show` открывает SQLite обычным
`sqlite3.connect`, а не URI `mode=ro`; до исправления это не forensic read-only
инструмент и его нельзя использовать для строгого аудита активной БД.

### Команды, меняющие состояние или выполняющие live-запросы

- `sudo yougile-start`, `sudo yougile-stop`, `sudo yougile-restart` — меняют
  systemd state;
- `yougile-runs continue|replay|trash-*` — меняют очередь или перемещают
  artifacts и требуют точного confirmation token;
- `recognize-wav -i ...` — ставит WAV в durable queue; `--force` сознательно
  обходит content deduplication;
- `yougile-cisnet ... --execute` — выполняет live CIS-Net search;
- `yougile-resolve` — делает live YouGile API requests, а interactive save может
  изменить EnvironmentFile;
- `src/yougile_webhooks.py --apply` — создаёт YouGile subscriptions;
- `src/baseline_yougile.py`, `src/backfill_wav.py`,
  `src/cisnet_automation.py --initialize` — migration/init actions;
- `yougile-reset-data` — безвозвратно удаляет runtime data.

Не запускайте их без отдельного разрешения, backup и понимания текущей очереди.

## Обычная эксплуатация

1. Проверьте `yougile-status` и `/health`.
2. Убедитесь, что активен ровно один receiver и один worker.
3. Для очереди и ошибок используйте privacy-safe `yougile-logs --once`.
4. Смотрите сохранённые run artifacts через ограниченный `yougile-runs view`,
   не через полный dump БД/JSONL.
5. Перед ручным retry установите причину остановки и уже сделанные ACRCloud
   attempts. Не создавайте новый recognition для того же WAV без необходимости.
6. Для CIS-Net убедитесь, что сотрудник не использует сессию. Автоматика должна
   завершиться Logout и оставить desktop/CDP остановленными.

### Правила ACRCloud

- code `1001` — допустимый API no-match, не ошибка транспорта;
- HTTP 429 и 5xx ограниченно retryable и должны учитывать backoff;
- ACR code `3003` означает исчерпание количества запросов: автоматического
  retry нет, продолжение допускается явно после восстановления лимита;
- ACR code `3015` означает превышение QPS и обрабатывается ограниченным retry с
  backoff;
- один ACRCloud candidate или `complete_candidates` не является verified result.

### Правила YouGile

- сохраняйте delivery/source/message deduplication;
- соблюдайте общий предел запросов и `Retry-After`;
- poller включайте только с явным scope; webhook и poller не должны создавать
  повторную обработку;
- result publication идёт через `chat_notifications`, не прямым вызовом CIS-Net
  к YouGile;
- не логируйте Authorization, payload или secret-bearing webhook URL.

### Правила CIS-Net

- сотрудник всегда имеет приоритет;
- при busy session нажимается только точная `No`; если её нет — fail closed;
- `Yes` используется только для подтверждения Logout;
- CDP/VNC/noVNC доступны только локально или через SSH tunnel;
- browser profile и cookies не входят в image/source archive.

## Backup и восстановление

### Консистентный backup

1. Зафиксируйте версии кода, конфигов и checksums, но не значения секретов.
2. В согласованное окно остановите target, timer/path и убедитесь, что receiver,
   worker, CIS-Net one-shot, desktop и CDP действительно inactive. Это требует
   отдельного разрешения.
3. После freeze выполните `quick_check` и `foreign_key_check` через URI
   `mode=ro`; для неподвижного snapshot допустим `immutable=1`.
4. Архивируйте целиком `data/`, включая DB/WAL/SHM, audio, recognition,
   aggregation и CIS-Net artifacts, с owner/group/mode/xattrs/ACL.
5. Храните secret files отдельно от state archive и source archive.
6. Запишите SHA-256 manifest и проверьте его после передачи.

### Восстановление

Восстанавливайте только в заранее подготовленный пустой target path при
остановленных сервисах. Сначала восстановите всю `data/` и права, затем
проверьте schema 12, SQLite integrity и наличие artifacts, и только после этого
разрешайте запуск. Не вызывайте initializer поверх отсутствующего snapshot:
пустая новая БД скрыла бы потерю истории и deduplication state.

Если это действительно новый пустой стенд, структура создаётся отдельно:
`incoming`, `audio`, `events`, `queue`, `recognition-runs`, `aggregation`,
`cisnet`; owner — runtime user, каталоги 750, per-run 700, файлы 600. Production
на 2026-09-23 пустой не был, поэтому этот путь не подходит для migration.

## Диагностика

- `/health` недоступен: проверьте unit state, localhost bind `8080`, journal и
  права на `data/`; не запускайте второй receiver для проверки.
- `database is locked`: найдите все процессы, использующие DB, проверьте local
  filesystem и отсутствие второго deployment; не удаляйте WAL/SHM.
- `Unsupported pipeline database schema`: остановитесь; не создавайте новую БД
  и не выполняйте произвольную migration.
- worker active, но не работает: проверьте `ACR_EXECUTE`, lock file, queue state
  и последние privacy-safe логи без вывода secret values.
- ACRCloud stop: изучите сохранённые attempts/status codes и бюджет; не
  перезапускайте обработку вслепую.
- CIS-Net busy: автоматика должна отложить run; не перехватывайте сессию
  сотрудника.
- CIS-Net browser не стартует: проверьте `DISPLAY=:99`, Xvfb socket,
  Playwright Chromium, права `cisbot` и localhost CDP `9223` без live login.
- aggregation export отсутствует: authoritative result остаётся в SQLite;
  проверьте aggregation state и export error, не пересоздавайте recognition.
- notification не ушёл: проверьте durable `chat_notifications`, receiver и
  YouGile rate-limit state; не публикуйте вручную дубликат.

## Разработка и offline checks

В production source не должны появляться secrets и runtime artifacts. Tests и
`offline/` сохраняются в source/CI, но не копируются в final image.

Безопасные статические проверки:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -c \
  'import ast,pathlib; [ast.parse(p.read_text(encoding="utf-8"), filename=str(p)) for root in (pathlib.Path("src"), pathlib.Path("tests"), pathlib.Path("offline")) for p in root.rglob("*.py") if "__pycache__" not in p.parts]'
node --check scripts/cisnet_search_works.js
node --check /opt/cisnet-playwright/launch-cdp.js
bash -n yougile-start yougile-stop yougile-restart yougile-status \
  yougile-logs yougile-httplogs yougile-cisnetlogs yougile-runs \
  recognize-wav yougile-cisnet
```

Полный unittest suite запускайте только в disposable environment с отключённой
сетью, без production secrets и с временной БД/данными:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  /opt/music-verifier/venvs/receiver/bin/python -m unittest discover -s tests -v
```

На production сервере эту команду не используйте. Перед запуском CI отдельно
докажите, что network transport подменён во всех выбранных tests.

## Docker Compose

Контейнерный комплект реализован в `Dockerfile`, `compose.yaml`, `docker/` и
соседнем `/opt/cisnet-playwright/Dockerfile`. Он содержит пять сервисов:

1. `init-data` — однократно создаёт чистый named volume, schema 12 и CIS-Net
   baseline; operational-таблицы остаются пустыми;
2. `receiver` — Uvicorn и YouGile intake, host port только `127.0.0.1:8080`;
3. `worker` — один ACRCloud worker и aggregation;
4. `cisnet-browser` — Xvfb, Fluxbox, Chromium/CDP и noVNC;
5. `cisnet-runner` — container-native замена systemd timer/path.

Оба runner-компонента используют один persistent volume
`music-verifier_music-data`. CDP `9223` доступен только внутри Compose network,
noVNC публикуется только на `127.0.0.1:6080`. Browser profile находится в
отдельном volume и не входит в image.

Корневой `.env` сохранён в Git с несекретными настройками и пустыми полями
секретов. Перед запуском на целевом сервере заполните только пустые значения и
установите mode 600; заполненный файл запрещено коммитить. `data/`, tests,
offline reference data, venv, node_modules, Git и временные artifacts исключены
из build context/final images.

Полная инструкция по сборке, первому запуску, чистой БД, проверкам, backup и
обновлению находится в `docs/DOCKER.md`. На текущем сервере Docker отсутствует,
поэтому выполнены статические проверки, но images и Compose stack здесь не
запускались.

## Checklist миграции и cutover

1. Установить Docker Engine/Compose на отдельном staging или новом сервере.
2. Собрать pinned images; прогнать syntax/unit/fake-API integration tests
   без внешней сети и restore rehearsal на обезличенном snapshot.
3. Зафиксировать source manifest, версии, текущие units, owners/modes, объём
   state, SQLite schema/integrity и rollback window.
4. Подготовить новый сервер, local persistent filesystem, non-root users,
   restricted firewall, localhost ports и secret injection.
5. Выполнить staging smoke: image starts, `/health`, FFmpeg/FFprobe, read-only
   SQLite, fake ACR transport, fixture webhook, outbox и browser desktop — без
   YouGile/ACRCloud/CIS-Net requests.
6. В согласованное окно freeze старый receiver, worker, timer/path и browser;
   подтвердить отсутствие writers и сделать snapshot всей `data/`.
7. Передать code/images, templates, secrets и state раздельно; проверить
   checksums, ownership и modes.
8. На новом сервере сначала только verify snapshot. Не инициализировать пустую
   production DB вместо потерянной.
9. Выполнить один cutover webhook/reverse proxy и запустить ровно один новый
   receiver/worker. Старый deployment оставить остановленным.
10. Наблюдать health, queue growth, duplicate indicators, ACR attempt budget,
    aggregation/CIS-Net/outbox и disk space.

Rollback: сначала полностью остановить новый deployment и вернуть ingress. Если
новый deployment уже записал события, нельзя одновременно запустить старый на
старом snapshot: нужно либо безопасно перенести обратно весь совместимый новый
state, либо согласовать replay/reconciliation. В каждый момент времени writers
должны существовать только на одной стороне.
