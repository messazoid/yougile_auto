# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG NODE_IMAGE=node:24-bookworm-slim

FROM ${NODE_IMAGE} AS node-runtime

FROM ${PYTHON_IMAGE} AS python-deps
WORKDIR /build
COPY config/receiver-requirements.lock config/acr-sdk-requirements.lock ./config/
COPY config/packages ./config/packages
RUN cd config/packages && sha256sum --check SHA256SUMS
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install \
        --no-index --find-links=/build/config/packages/receiver \
        -r /build/config/receiver-requirements.lock \
    && /opt/venv/bin/python -m pip install \
        --no-index --no-deps --find-links=/build/config/packages/acr-sdk \
        pyacrcloud==1.0.12 \
    && /opt/venv/bin/python -m pip check

FROM ${PYTHON_IMAGE} AS music-runtime
ARG APP_UID=10001
ARG APP_GID=10001
RUN test "$(dpkg --print-architecture)" = amd64 \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" music \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --home-dir /opt/music-verifier \
        --no-create-home --shell /usr/sbin/nologin music
COPY --from=python-deps /opt/venv /opt/venv
WORKDIR /opt/music-verifier
COPY --chown=${APP_UID}:${APP_GID} src ./src
COPY --chown=${APP_UID}:${APP_GID} scripts/cisnet_search_works.js ./scripts/cisnet_search_works.js
COPY --chown=${APP_UID}:${APP_GID} docker ./docker
RUN chmod 0755 docker/cisnet-wrapper.py
ENV PATH=/opt/venv/bin:${PATH} \
    PYTHONPATH=/opt/music-verifier/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp
USER ${APP_UID}:${APP_GID}
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "docker/worker-entrypoint.py"]

FROM music-runtime AS cisnet-runner
ARG APP_UID=10001
ARG APP_GID=10001
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
COPY docker/cisnet-runner/package.json docker/cisnet-runner/package-lock.json ./
RUN PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm ci --omit=dev --ignore-scripts \
    && npm cache clean --force
USER ${APP_UID}:${APP_GID}
CMD ["python", "docker/cisnet-loop.py"]
