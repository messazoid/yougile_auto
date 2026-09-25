# Music Verifier: Docker deployment copy

This directory is the self-contained deployment source for Music Verifier and
its CIS-Net browser. `compose.yaml` builds both images from this directory:
the application from `Dockerfile`, the browser from `cisnet-playwright/Dockerfile`.
There are no host service units or host runtime wrappers in this copy.

## Contents

- `src/`, `scripts/`, `docker/`: application and container entrypoints;
- `cisnet-playwright/`: browser image source;
- `config/`: dependency locks and the offline Python wheelhouse;
- `.env`: nonsecret template; `compose.yaml`: five services, application data
  in `./data` for new installs, and a named browser-profile volume;
- `bin/`: optional Compose command wrappers;
- `tests/`: source and runtime checks.

The source server's `data/`, virtual environments, browser profile, and working
credentials are not in this copy. New installations created from `.env` put
application state in the Git-ignored `./data` directory. Existing protected env
files without `MUSIC_DATA_SOURCE` continue using the named `music-data` volume
until they are deliberately migrated. To retain existing production history,
restore a consistent snapshot of the whole data tree before `up -d`. Never run this
stack and the existing receiver or worker against the same YouGile scope.

## Build and start

Install Docker Engine with the Compose plugin on the target host, then create a
protected environment file from `.env`. Fill the required credentials and scope
settings in that protected file. Do not commit or print it.

```bash
cd /opt/music-verifier-docker
install -o root -g root -m 600 .env /etc/music-verifier.env
# Edit /etc/music-verifier.env securely.
docker compose --env-file /etc/music-verifier.env config --quiet
docker compose --env-file /etc/music-verifier.env build
# If preserving existing history, restore the stopped data snapshot now.
docker compose --env-file /etc/music-verifier.env up -d
docker compose --env-file /etc/music-verifier.env ps -a
curl --fail http://127.0.0.1:8080/health
```

The example template leaves ACRCloud execution, CIS-Net automation, and YouGile
polling disabled. Manual CIS-Net searches still require `--execute`.
The receiver listens on host loopback port 8080; noVNC listens on host loopback
port 6080. Put your HTTPS proxy in front of the receiver as appropriate for the
target host. Configure webhooks and enable paid recognition only after checking
scope, state, and one controlled cutover.

For a history-preserving migration, follow [docs/DOCKER.md](docs/DOCKER.md)
before running `up -d`. It also lists command equivalents and checks.

## CIS-Net browser driver

Both the browser launcher and the CDP runner use **Patchright 1.62.1**. The
browser remains Chromium. The Microsoft Playwright base image supplies the
system dependencies; the browser Dockerfile also runs Patchright's Chromium
installer to ensure that the browser revision matches the locked driver.
Update both npm manifests and lockfiles together when upgrading the driver.

Existing directory names, the `cisnet-profile` volume, and the
`CISNET_PLAYWRIGHT_MODULE` environment variable retain their names for deployment
compatibility. That variable now points to `node_modules/patchright`. Do not
attach an ordinary Playwright client when assessing Patchright's behavior.
Patchright does not guarantee that automation cannot be detected.
The adapter keeps its result observer in a `JSHandle` rather than `window`
properties, so polling uses the observer's own execution context and does not
depend on globals being shared between Patchright's isolated contexts.

To check the adapter against a local HTML fixture, with no external network,
credentials, or existing browser profile:

```bash
# Run from the repository root after building cisnet-browser.
docker run --rm --init --network none --read-only \
  --tmpfs /tmp:size=512m,mode=1777 --shm-size=1g \
  --tmpfs /var/lib/cisnet-playwright:uid=1001,gid=1001,mode=0700 \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --entrypoint /usr/bin/xvfb-run cisnet-playwright:local \
  -a node --test /workspace/tests/cisnet_adapter_smoke.cjs
```

The fixture covers login, repeated searches, ISWC search, pagination, empty
results, and logout through the actual adapter in separate CDP clients. It also
checks `navigator.webdriver` and whether adapter globals are visible to the
page's own JavaScript. This is a compatibility check, not a test of CIS-Net's
detection systems.

To deploy an update, first finish any active CIS-Net operation, then rebuild
and recreate both CIS-Net services. Retain the existing profile volume:

```bash
docker compose --env-file /etc/music-verifier.env build cisnet-browser cisnet-runner
docker compose --env-file /etc/music-verifier.env stop cisnet-runner
docker compose --env-file /etc/music-verifier.env up -d --no-deps --wait cisnet-browser cisnet-runner
```
