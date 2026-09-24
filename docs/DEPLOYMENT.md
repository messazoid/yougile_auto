# Deployment checklist

1. Transfer the entire `/opt/music-verifier-docker` directory to the target
   Docker host. It includes both application and CIS-Net browser build sources.
2. Install Docker Engine and Compose plugin, then create a root-readable
   protected `/etc/music-verifier.env` from the nonsecret `.env` template.
3. Set the target's YouGile scope and webhook base, ACRCloud credentials,
   CIS-Net credentials, and VNC password. Keep polling and paid recognition
   disabled during initial checks.
4. Run `docker compose --env-file /etc/music-verifier.env config --quiet` and
   `build`. If retaining history, stop the old writers and restore a consistent
   whole-data snapshot into an empty Docker volume before `up -d`.
5. Start the stack with `up -d`. Check `ps -a`, receiver `/health`, the SQLite
   healthcheck, and the browser's noVNC page through a local or SSH tunnel.
6. Configure HTTPS routing to `127.0.0.1:8080` on the target host. Apply webhook
   subscriptions only for the intended YouGile scope.
7. Run one controlled end-to-end task. Enable polling, ACRCloud recognition, and
   CIS-Net automation only for the intended scope and after the old writers are
   stopped. Manual CIS-Net searches remain separately explicit.

The detailed commands and state precautions are in `README.md` and
`docs/DOCKER.md`. This bundle has not been built on this source host because
Docker is not installed here.
