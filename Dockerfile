# syntax=docker/dockerfile:1
# Cortensor Canary runs on the standard library, so the image stays tiny and
# the default target (demo) needs no credentials and no network.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY config ./config
COPY canary ./

RUN chmod +x /app/canary \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

# Non-root: the gate only writes into /app/artifacts.
RUN useradd --create-home --shell /usr/sbin/nologin canary \
    && mkdir -p /app/artifacts \
    && chown -R canary:canary /app
USER canary

# `docker run <image>` runs the offline demo; override the command for a real gate:
#   docker run --rm -e CORTENSOR_API_KEY -v "$PWD/config:/app/config:ro" <image> \
#     canary --baseline local --candidate portal --suite support-ru
CMD ["canary", "demo", "--workspace", "/app/artifacts/demo"]
