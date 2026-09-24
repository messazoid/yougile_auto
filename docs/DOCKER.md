# Docker operations

Run these commands from `/opt/music-verifier-docker`. Use a protected environment
file such as `/etc/music-verifier.env`; `docker compose config` without `--quiet`
can print credentials. The tracked `.env` is only a nonsecret template.

```bash
cd /opt/music-verifier-docker
docker compose --env-file /etc/music-verifier.env config --quiet
docker compose --env-file /etc/music-verifier.env build
# If preserving existing history, restore the stopped data snapshot now.
docker compose --env-file /etc/music-verifier.env up -d
docker compose --env-file /etc/music-verifier.env ps -a
docker compose --env-file /etc/music-verifier.env logs --tail=100 receiver worker cisnet-runner cisnet-browser
```

The `.env` template sets `MUSIC_DATA_SOURCE=./data`, so new installations keep
application state in the checkout's Git-ignored `data/` directory. Existing
protected env files without this setting continue using the named `music-data`
volume. `init-data` creates a clean SQLite database in an empty data directory. It
also accepts a restored, unmarked directory after checking SQLite integrity, the
current schema, and the CIS-Net baseline. It will not migrate an old schema or
replace a missing database. `cisnet-profile` holds the browser profile.
The data directory and browser volume survive container replacement. Never use
`down -v` unless deleting named volumes, including the browser profile and any
legacy application data, is explicitly intended.

If an existing `cisnet-profile` volume was first used by a browser container
with an automatically generated hostname, Chromium can refuse to open it after
the container is recreated. Its error mentions `SingletonLock` and "another
computer". Stop the browser, remove only its three singleton symlinks, then
recreate the browser and runner:

```bash
docker compose --env-file /etc/music-verifier.env stop cisnet-runner cisnet-browser
docker compose --env-file /etc/music-verifier.env run --rm --no-deps \
  --entrypoint bash cisnet-browser -c \
  'for name in SingletonLock SingletonSocket SingletonCookie; do path="$HOME/cdp-profile/$name"; if [[ -L "$path" ]]; then rm -- "$path"; fi; done'
docker compose --env-file /etc/music-verifier.env up -d --force-recreate cisnet-browser cisnet-runner
docker compose --env-file /etc/music-verifier.env ps -a
```

The Compose browser has a fixed hostname, so future recreations use the same
hostname when Chromium checks its profile lock. The recovery command leaves
the profile data, session, and the `music-data` volume untouched.

The image has five services: `init-data`, `receiver`, `worker`, `cisnet-browser`,
and `cisnet-runner`. Receiver and worker share one data volume. CDP is reachable
only on the browser container's loopback; the runner shares that container's
network namespace and connects to `127.0.0.1:9223`. Its health check verifies
that CDP is reachable there. Receiver and noVNC host ports bind to loopback.
The browser image is built from `./cisnet-playwright`, so this directory can be
moved as one unit. Application container paths remain `/opt/music-verifier` and
browser container paths remain `/opt/cisnet-playwright` regardless of host path.
With the template environment, `YOUGILE_POLL_ENABLED=false`, `ACR_EXECUTE=0`,
and `CISNET_EXECUTE=0` prevent automatic external processing on initial start.

## Checks

```bash
curl --fail http://127.0.0.1:8080/health
docker compose --env-file /etc/music-verifier.env exec -T receiver python docker/healthcheck.py database
docker compose --env-file /etc/music-verifier.env ps -a
```

noVNC is available at `http://127.0.0.1:6080/vnc.html` through a local session
or SSH tunnel. Before a real cutover, validate image startup and the browser on
a Docker host. This source server does not currently have Docker installed.

## Operator commands

`bin/yougile-compose` dispatches the `yougile-*` commands through Compose. For a
local command directory, install symlinks without changing global commands:

```bash
YOUGILE_COMMAND_DIR=/opt/music-verifier-docker/commands \
  /opt/music-verifier-docker/bin/install-compose-commands
```

The dispatcher finds this copy from its own location. It reads
`/etc/music-verifier.env` by default and can be directed to a different
protected file with `YOUGILE_ENV_FILE`. Global command installation should be
done only as part of the cutover.

| Previous command | Compose equivalent |
| --- | --- |
| `yougile-start`, `yougile-stop`, `yougile-restart` | Start the stack, stop application workers, or recreate application workers; the browser stays available on stop/restart |
| `yougile-status [--full]` | Show container states; `--full` also shows processes |
| `yougile-logs [--once\|--follow] [--all]` | Read redacted application and browser logs |
| `yougile-httplogs [--once\|--follow]` | Read redacted YouGile HTTP events |
| `yougile-cisnetlogs [--once\|--follow]` | Read CIS-Net container logs |
| `yougile-runs`, `yougile-resolve`, `yougile-cisnet` | Run the corresponding CLI inside a container |
| `recognize-wav -i FILE [--force]` | Copy the WAV temporarily into the worker and queue it |
| `yougile-env` | Edit the protected host environment file |
| `yougile-reset-data` | Print the plan; `--execute 'RESET MUSIC-VERIFIER DATA'` clears active application data after checking its mount |

The command installer refuses to overwrite existing host commands. The reset
command stops the Compose stack, then empties only this checkout's active `data/`
directory or removes the active named application data volume. It verifies all
application services share that mount, refuses another bind path, a symlink, or
a nested mount, and preserves the browser profile volume and any inactive old
data volume. It leaves the stack stopped. Run `yougile-start` to initialize a
new queue after an intentional reset. No confirmation is needed for the
no-argument plan; execution requires the exact phrase shown there.
`recognize-wav` requires a running worker container.
`yougile-resolve --columns` reads YouGile settings from the receiver container,
then saves selected column IDs into the protected host environment file. Run
`yougile-restart` when ready to apply the changed scope to the receiver.

## State and backup

### Move an existing named volume into `./data`

An existing server continues using its named volume after this code update.
Migrate it during a short maintenance window, with no running commands writing
to the queue. From the checkout root, first confirm that the target path is
absent and has enough free space for a second full copy of the volume:

```bash
source_path="$(docker volume inspect music-verifier_music-data --format '{{.Mountpoint}}')"
du -sh "$source_path"
df -h .
test ! -e data && test ! -L data
```

Only proceed when the copy fits on the target filesystem. Then:

```bash
(
  set -euo pipefail
  docker compose --env-file /etc/music-verifier.env stop receiver worker cisnet-runner
  source_path="$(docker volume inspect music-verifier_music-data --format '{{.Mountpoint}}')"
  test -f "$source_path/queue/pipeline.sqlite3"
  test ! -e data && test ! -L data
  mkdir data
  cp -a "$source_path"/. data/
  diff -qr "$source_path" data
)
```

The `diff` must succeed before changing the protected environment file. Add
`MUSIC_DATA_SOURCE=./data` to `/etc/music-verifier.env` without printing its
contents. Then check and start the services:

```bash
docker compose --env-file /etc/music-verifier.env config --quiet
docker compose --env-file /etc/music-verifier.env up -d init-data receiver worker cisnet-runner
docker compose --env-file /etc/music-verifier.env ps -a
```

Check a known existing run with `yougile-runs show RUN_ID`. Keep the old named volume as a
rollback copy; do not remove it during migration. The local
`compose.override.yaml` still applies, including any DNS setting for `worker`.
If startup fails after migration,
remove `MUSIC_DATA_SOURCE=./data` from the protected environment file and run
`docker compose --env-file /etc/music-verifier.env up -d`; this reattaches the
unchanged named volume. Keep the copied `data/` directory until the cause is
understood.

### Restore a snapshot into the original named volume

The copy contains no production state or credentials. A fresh start creates a
new database and browser profile. For existing history, stop all source writers
first, then copy the *entire* source `data/` tree, including SQLite WAL/SHM,
audio, runs, and results, to a protected directory on the target host. After
building the image and before `up -d`, restore that directory into a new, empty
volume. This legacy volume workflow requires `MUSIC_DATA_SOURCE=music-data` in
the protected environment file. For the default image and project names:

```bash
set -o pipefail
test -d /path/to/stopped-data-snapshot
docker volume create music-verifier_music-data
docker run --rm --user 0 \
  -v music-verifier_music-data:/opt/music-verifier/data \
  --entrypoint sh music-verifier:local \
  -c 'test -z "$(ls -A /opt/music-verifier/data)"'
tar -C /path/to/stopped-data-snapshot -cf - . |
  docker run --rm -i --user 0 \
    -v music-verifier_music-data:/opt/music-verifier/data \
    --entrypoint tar music-verifier:local \
    -C /opt/music-verifier/data -xf -
docker compose --env-file /etc/music-verifier.env up -d
docker compose --env-file /etc/music-verifier.env exec -T receiver \
  python docker/healthcheck.py database
```

Run this only against an empty volume. On first start, `init-data` verifies the
restored database before it creates its marker and adjusts file ownership. A
failed schema or integrity check leaves the application stopped. The browser
profile and environment file need separate protected backups. Never start two
workers or pollers for the same scope at once.
