FROM python:3.12-slim-bookworm

ARG UPSTREAM_REPO=https://github.com/DanielWTE/ebay-kleinanzeigen-api.git
# Pinned deliberately. We import upstream's scraper functions directly rather
# than going through its HTTP API, so an unreviewed upstream refactor would
# break this image at runtime. Bump this, rebuild, smoke-test, then ship.
#
# 2026-09-07 bump (1129536 -> da2fb02): Kleinanzeigen's Astro relaunch (early
# Sept) dropped the old `.ad-listitem` card markup; upstream's own fix
# (11781a9, "restore listing detection after Kleinanzeigen Astro relaunch")
# restores adid/url/title/location but still misses price, description and
# date, so patches/astro-results-fields.patch (applied below) adds fallbacks
# for those. Keep the patch in sync when moving the pin — `git apply --check`
# fails loudly on drift.
ARG UPSTREAM_SHA=da2fb0204198253e6b798c0a4cadb06e73fd2438

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    PYTHONPATH=/app/upstream

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY patches /tmp/patches

# Blob-filtered clone: fast, but unlike --depth=1 it can still check out an
# arbitrary commit. Apply our patch while .git still exists (`git apply`
# needs it); --check fails the build loudly if the pinned checkout drifts.
RUN git clone --filter=blob:none "${UPSTREAM_REPO}" upstream \
    && git -C upstream checkout --quiet "${UPSTREAM_SHA}" \
    && git -C upstream apply --check /tmp/patches/astro-results-fields.patch \
    && git -C upstream apply /tmp/patches/astro-results-fields.patch \
    && rm -rf upstream/.git upstream/tests /tmp/patches

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
