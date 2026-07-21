# kleinanzeigen-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an LLM search
[Kleinanzeigen.de](https://www.kleinanzeigen.de), Germany's largest classifieds
site. Built for self-hosting: one container, no API keys, no per-query credits.

Scraping is done by [DanielWTE/ebay-kleinanzeigen-api][upstream], which is baked
into the image at a pinned commit. This project is the MCP layer on top.

[upstream]: https://github.com/DanielWTE/ebay-kleinanzeigen-api

## Tools

| Tool | What it does |
|------|--------------|
| `search_listings` | Search by keyword, location, radius and price range. Returns listing summaries. |
| `get_listing` | Full detail page for one listing id. |
| `get_listings_batch` | Full details for several ids at once — the normal follow-up to a search. |
| `search_by_url` | Search from a pasted Kleinanzeigen URL, preserving filters that `search_listings` cannot express (vehicle make, model year, room count, …). |
| `parse_search_url` | Explain which filters a URL encodes. Makes no network request. |

The intended flow is `search_listings` → pick interesting ids →
`get_listings_batch`. Search results deliberately omit descriptions so a broad
search does not blow up the model's context window.

## Running it

```bash
docker run --rm -p 8000:8000 ghcr.io/jnslmk/kleinanzeigen-mcp:latest
```

The server speaks streamable HTTP at `http://localhost:8000/mcp`, with a plain
`GET /healthz` for container healthchecks. Set `MCP_TRANSPORT=stdio` to run it
as a local stdio server instead.

Chromium needs room to work — give the container at least 1.5 GB of memory.

### Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `MCP_TRANSPORT` | `http` | `http` (streamable HTTP) or `stdio` |
| `MCP_HOST` | `0.0.0.0` | Bind address |
| `MCP_PORT` | `8000` | Bind port |
| `MCP_PATH` | `/mcp` | MCP endpoint path |
| `KZ_MAX_CONTEXTS` | `4` | Chromium browser contexts to pool |
| `KZ_MAX_CONCURRENT` | `2` | Concurrent scrapes |
| `KZ_MAX_PAGE_COUNT` | `5` | Cap on pages per search (~25 listings each) |
| `KZ_MAX_BATCH_SIZE` | `20` | Cap on ids per `get_listings_batch` call |
| `LOG_LEVEL` | `INFO` | Python log level |

The `KZ_MAX_*` defaults are tuned for a chat agent making one request at a time.
Raise them for throughput, at the cost of memory and a greater chance of
tripping Kleinanzeigen's bot detection.

### LibreChat

```yaml
mcpServers:
  kleinanzeigen:
    type: streamable-http
    url: "http://kleinanzeigen-mcp:8000/mcp"
    timeout: 120000
    chatMenu: true

mcpSettings:
  allowedAddresses:
    - "kleinanzeigen-mcp:8000"
```

`allowedAddresses` is required: LibreChat's SSRF guard blocks MCP URLs that
resolve to private addresses, which a sibling container always does. The
timeout is generous on purpose — a cold search has to start a browser context
and load real pages.

### Claude Code

```bash
claude mcp add --transport http kleinanzeigen http://localhost:8000/mcp
```

## Development

```bash
uv venv && uv pip install -e .
# The scrapers are not on PyPI, so point PYTHONPATH at a checkout of upstream:
git clone https://github.com/DanielWTE/ebay-kleinanzeigen-api.git /tmp/kz-api
pip install -r /tmp/kz-api/requirements.txt && playwright install chromium
PYTHONPATH=/tmp/kz-api python -m kleinanzeigen_mcp
```

### Upgrading the scraper

`UPSTREAM_SHA` in the `Dockerfile` pins the upstream commit. It is pinned rather
than tracking `main` because this server imports upstream's scraper functions
directly — `ultra_optimized_scrape_inserate`, `get_inserate_details_optimized`
and `scrape_by_url` — instead of going through its HTTP API. That avoids a
second process and an HTTP hop, but it means an upstream refactor can break this
image at runtime rather than at build time. Bump the SHA, rebuild, smoke-test
each tool, then ship.

## Images

`ghcr.io/jnslmk/kleinanzeigen-mcp` — multi-arch (`linux/amd64`, `linux/arm64`),
built by GitHub Actions on native runners for each architecture.

| Tag | Meaning |
|-----|---------|
| `latest` | Newest build of `main` |
| `sha-<full-sha>` | A specific commit |
| `v1.2.3`, `v1.2` | Release tags |

## Caveats

Kleinanzeigen has no public API, so this scrapes the site with a headless
browser. That means it can break whenever they change their markup, and heavy
or parallel use may trip bot detection — particularly from a datacenter IP.
Scraping is also at odds with Kleinanzeigen's terms of service. Keep it to
personal-scale use.

## License

MIT. Bundles [DanielWTE/ebay-kleinanzeigen-api][upstream], also MIT — see
[LICENSE](LICENSE).
