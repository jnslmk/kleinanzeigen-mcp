FROM python:3.12-slim-bookworm

ARG UPSTREAM_REPO=https://github.com/DanielWTE/ebay-kleinanzeigen-api.git
# Pinned deliberately. We import upstream's scraper functions directly rather
# than going through its HTTP API, so an unreviewed upstream refactor would
# break this image at runtime. Bump this, rebuild, smoke-test, then ship.
ARG UPSTREAM_SHA=1129536bb10e4d1c3b06e295beac0fe3f36f2a7d

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    PYTHONPATH=/app/upstream

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Blob-filtered clone: fast, but unlike --depth=1 it can still check out an
# arbitrary commit.
RUN git clone --filter=blob:none "${UPSTREAM_REPO}" upstream \
    && git -C upstream checkout --quiet "${UPSTREAM_SHA}" \
    && rm -rf upstream/.git upstream/tests

RUN pip install -r upstream/requirements.txt

# Ahead of the source copy: a ~100 MB browser download should not be redone
# every time this project's own code changes.
RUN playwright install --with-deps chromium

COPY pyproject.toml README.md ./
COPY kleinanzeigen_mcp ./kleinanzeigen_mcp
RUN pip install .

# Chromium lives in PLAYWRIGHT_BROWSERS_PATH, which root just wrote to; hand it
# to the unprivileged user the container actually runs as.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin mcp \
    && chown -R mcp:mcp /opt/playwright
USER mcp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5).status == 200 else 1)"

CMD ["kleinanzeigen-mcp"]
