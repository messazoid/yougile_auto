# syntax=docker/dockerfile:1.7

ARG PLAYWRIGHT_IMAGE=mcr.microsoft.com/playwright:v1.62.1-noble
FROM ${PLAYWRIGHT_IMAGE}

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl fluxbox novnc tini websockify x11vnc xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/cisnet-playwright
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts \
    && npm cache clean --force
COPY launch-cdp.js cisnet-desktop.sh container-entrypoint.sh container-healthcheck.sh ./
RUN install -d -o pwuser -g pwuser -m 0700 \
        /var/lib/cisnet-playwright /var/lib/cisnet-playwright/.vnc \
        /var/lib/cisnet-playwright/cdp-profile \
    && chown -R pwuser:pwuser /opt/cisnet-playwright \
    && chmod 0755 cisnet-desktop.sh container-entrypoint.sh container-healthcheck.sh

ENV HOME=/var/lib/cisnet-playwright \
    DISPLAY=:99 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    CISNET_CDP_BIND_ADDRESS=127.0.0.1 \
    CISNET_CDP_PORT=9223 \
    NOVNC_BIND_ADDRESS=127.0.0.1

USER pwuser
EXPOSE 6080 9223
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/cisnet-playwright/container-entrypoint.sh"]
HEALTHCHECK --interval=15s --timeout=5s --start-period=45s --retries=8 \
    CMD ["/opt/cisnet-playwright/container-healthcheck.sh"]
