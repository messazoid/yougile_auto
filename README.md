# Music Verifier: Docker deployment copy

This directory is the self-contained deployment source for Music Verifier and
its CIS-Net browser. `compose.yaml` builds both images from this directory:
the application from `Dockerfile`, the browser from `cisnet-playwright/Dockerfile`.
There are no host service units or host runtime wrappers in this copy.

## Contents

- `src/`, `scripts/`, `docker/`: application and container entrypoints;
- `cisnet-playwright/`: browser image source;
- `config/`: dependency locks and the offline Python wheelhouse;
- `.env`: nonsecret template; `compose.yaml`: five services and two persistent
  named volumes;
- `bin/`: optional Compose command wrappers;
- `tests/`: source and runtime checks.

The source server's `data/`, virtual environments, browser profile, and working
credentials are not in this copy. Docker creates fresh named volumes on first
start. To retain existing production history, restore a consistent snapshot of
the whole data tree into an empty volume before `up -d`. Never run this
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
