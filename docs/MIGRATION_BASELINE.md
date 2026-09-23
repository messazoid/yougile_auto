# Migration baseline

Снимок подготовлен 2026-09-23 для последующей контейнеризации и переноса
YouGile Music Verifier вместе с CIS-Net Playwright.

Этот документ фиксирует границы исходного комплекта. Фактическая эксплуатация
описана в `README.md`, а полный аудит сохранён отдельно от репозитория в
`/opt/yougile-music-verifier-cisnet-audit-2026-09-23.md`.

## Репозитории

Исходники фиксируются двумя независимыми локальными Git-репозиториями:

- `/opt/music-verifier` — Python pipeline, команды, tests, конфигурационные
  шаблоны и systemd deployment files;
- `/opt/cisnet-playwright` — Node/Playwright, Chromium launcher и desktop
  scripts.

Remote для репозиториев не настроен. Перед передачей на рабочий сервер нужно
выбрать корпоративное Git-хранилище либо сформировать проверяемый release
archive.

## Что входит в исходный комплект

Music Verifier:

- `src/`, `scripts/`, корневые операторские wrappers;
- `tests/` и `offline/` для CI и проверки, но не для final image;
- `config/*requirements*`, `config/packages/` и `config/*.env.example`;
- `deploy/`, `docs/`, `README.md`, `AGENTS.md`.

CIS-Net Playwright:

- `launch-cdp.js`, `cisnet-desktop.sh`, `navigate-download.js`;
- `run-navigate-download.sh`, `package.json`, `package-lock.json`;
- `PROJECT_STATUS.md`, `AGENTS.md`.

## Что не входит в Git и container build context

- production `data/` и SQLite;
- secret-bearing `.env`;
- Python venv, `node_modules` и browser cache/profile;
- `__pycache__`, test caches, Playwright diagnostics;
- backup-каталоги, временные файлы, логи и screenshots.

Игнорирование не означает удаление. Эти файлы остаются на текущем сервере до
отдельного решения.

## Persistent state и секреты

Вся `/opt/music-verifier/data/` переносится отдельно согласованным snapshot
после остановки всех writers. Нельзя переносить только
`data/queue/pipeline.sqlite3`: WAL/SHM, audio, recognition, aggregation и
CIS-Net artifacts образуют единое состояние.

Секреты передаются отдельно и не входят в Git или image:

- `/etc/yougile-video.env`;
- `/etc/music-verifier-worker.env`;
- `/opt/cisnet-playwright/config/.env`;
- `/var/lib/cisnet-playwright/.vnc/passwd`.

## Текущий runtime-контракт

- receiver: `yougile:yougile`, `/opt/music-verifier`, localhost `8080`;
- worker: `yougile:yougile`, отдельный ACR SDK venv;
- CIS-Net automation: systemd oneshot от root, timer и path activation;
- desktop: `cisbot:cisbot`, Xvfb/Fluxbox/VNC/noVNC;
- transient CDP browser: localhost `9223`.

Кроме файлов `deploy/`, текущая установка зависит от:

- `music-verifier-worker.service.d/aggregation.conf`;
- `music-verifier-cisnet.service.d/override.conf`;
- `cisnet-desktop.service`.

Их поведение нужно перенести в Compose, а не копировать неявно.

## Зафиксированные версии

- Python 3.12.3;
- receiver pip 26.2.1;
- pyacrcloud 1.0.12;
- Node.js 24.21.0;
- npm 11.19.0;
- Playwright 1.62.1;
- FFmpeg 6.1.1.

Native ACRCloud library подтверждена только для x86_64/glibc. Целевую
архитектуру сервера нужно проверить до сборки image.

## Открытые решения перед контейнеризацией

- корпоративный Git remote и правила release tags;
- TLS/reverse proxy для webhook;
- UID/GID пользователей на новом сервере;
- способ инъекции Docker secrets;
- backup retention и rollback window;
- container-native supervisor и heartbeat CIS-Net;
- heartbeat worker;
- актуальная трактовка ACRCloud codes 3003/3015.
