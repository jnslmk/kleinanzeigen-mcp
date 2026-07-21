"""MCP server exposing Kleinanzeigen.de listing search as tools.

The scraping itself is done by DanielWTE/ebay-kleinanzeigen-api, which is baked
into the image at a pinned commit and imported directly (see the Dockerfile).
Upstream ships a FastAPI app; we skip it entirely and drive the scraper
functions ourselves, so there is no second process and no HTTP hop.

Upstream's scrapers take an ``OptimizedPlaywrightManager`` as an explicit first
argument, so the only thing this module has to own is that manager's lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

# Upstream (DanielWTE/ebay-kleinanzeigen-api) — importable because the image
# puts its checkout on PYTHONPATH. Upstream is not a installable package; these
# are its top-level modules.
from scrapers.inserat import get_inserate_details_optimized
from scrapers.inserate_by_url import scrape_by_url
from scrapers.inserate_ultra_optimized import ultra_optimized_scrape_inserate
from utils.browser import OptimizedPlaywrightManager
from utils.parse_kleinanzeigen_url import (
    map_to_inserate_params,
    parse_kleinanzeigen_url,
)

log = logging.getLogger("kleinanzeigen-mcp")

# Chromium contexts are the memory hog here — each one is a browser profile.
# Upstream's FastAPI app defaults to 20/10, which is sized for a throughput API;
# an MCP server backing a chat agent handles one request at a time, so we run
# far leaner by default and let the deployment raise it.
MAX_CONTEXTS = int(os.getenv("KZ_MAX_CONTEXTS", "4"))
MAX_CONCURRENT = int(os.getenv("KZ_MAX_CONCURRENT", "2"))

# Search responses are fed straight into an LLM context window, so page_count is
# capped well below upstream's limit of 20 (~25 listings per page).
MAX_PAGE_COUNT = int(os.getenv("KZ_MAX_PAGE_COUNT", "5"))

# Detail fetches open a page each; more than a few in parallel trips
# Kleinanzeigen's bot detection.
MAX_BATCH_SIZE = int(os.getenv("KZ_MAX_BATCH_SIZE", "20"))

_browser: OptimizedPlaywrightManager | None = None


def _manager() -> OptimizedPlaywrightManager:
    if _browser is None:  # pragma: no cover - guarded by the lifespan
        raise RuntimeError("Browser manager is not running")
    return _browser


@asynccontextmanager
async def lifespan(_: FastMCP) -> AsyncIterator[None]:
    """Start one shared Chromium instance for the process lifetime."""
    global _browser
    _browser = OptimizedPlaywrightManager(
        max_contexts=MAX_CONTEXTS, max_concurrent=MAX_CONCURRENT
    )
    await _browser.start()
    log.info(
        "Chromium ready (max_contexts=%s, max_concurrent=%s)",
        MAX_CONTEXTS,
        MAX_CONCURRENT,
    )
    try:
        yield
    finally:
        await _browser.close()
        _browser = None


mcp = FastMCP(
    name="kleinanzeigen",
    version="0.1.0",
    lifespan=lifespan,
    instructions=(
        "Search Kleinanzeigen.de, Germany's largest classifieds site, for "
        "second-hand listings. Start with `search_listings` (or "
        "`search_by_url` if the user pasted a Kleinanzeigen search URL) to get "
        "listing IDs and summaries, then call `get_listings_batch` for the full "
        "description, seller and image data of the ones worth a closer look. "
        "Prices are in EUR and a price of 0 usually means 'zu verschenken' "
        "(free) or 'VB' with no figure given."
    ),
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

# Upstream annotates every response with timing/throughput data. It is useless
# to an LLM and costs tokens on every single call, so it gets stripped.
_NOISE_KEYS = (
    "performance_metrics",
    "task_metrics",
    "optimization_features",
    "browser_metrics",
    "time_taken",
)


def _slim(payload: Any) -> Any:
    """Recursively drop upstream's performance telemetry from a response."""
    if isinstance(payload, dict):
        return {k: _slim(v) for k, v in payload.items() if k not in _NOISE_KEYS}
    if isinstance(payload, list):
        return [_slim(v) for v in payload]
    return payload


def _normalize_search(result: dict[str, Any]) -> dict[str, Any]:
    """Rename the search result id field to match what the detail tools expect.

    Upstream calls it ``adid`` when searching but ``id`` on the detail page.
    Handing an LLM two names for one identifier reliably produces calls to
    `get_listings_batch` with the wrong field, so settle on ``id`` throughout.
    """
    out = _slim(result)
    for listing in out.get("results") or []:
        if isinstance(listing, dict) and "adid" in listing:
            listing["id"] = listing.pop("adid")
    return out


def _parse_date(value: str | None, field: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"{field} must be an ISO 8601 datetime such as 2026-07-01T00:00:00"
        ) from exc


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #


@mcp.tool
async def search_listings(
    query: Annotated[
        str | None,
        Field(description="Search terms, e.g. 'ThinkPad T14' or 'Fahrrad 28 Zoll'"),
    ] = None,
    location: Annotated[
        str | None,
        Field(description="City name or German postal code, e.g. 'Berlin' or '10115'"),
    ] = None,
    radius_km: Annotated[
        int | None,
        Field(description="Search radius around `location` in kilometres", ge=0),
    ] = None,
    min_price: Annotated[int | None, Field(description="Minimum price in EUR", ge=0)] = None,
    max_price: Annotated[int | None, Field(description="Maximum price in EUR", ge=0)] = None,
    page_count: Annotated[
        int,
        Field(description="Result pages to fetch, ~25 listings each", ge=1),
    ] = 1,
    published_after: Annotated[
        str | None,
        Field(
            description=(
                "Only return listings published at or after this ISO 8601 "
                "datetime, e.g. '2026-07-01T00:00:00'"
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Search Kleinanzeigen listings by keyword, location and price.

    Returns listing summaries — id, title, price, location, thumbnail and URL.
    Use `get_listings_batch` with the returned ids to get full descriptions and
    seller details. Every filter is optional, but a search with no `query` and
    no `location` returns an arbitrary slice of the site and is rarely useful.
    """
    page_count = min(page_count, MAX_PAGE_COUNT)
    result = await ultra_optimized_scrape_inserate(
        browser_manager=_manager(),
        query=query,
        location=location,
        radius=radius_km,
        min_price=min_price,
        max_price=max_price,
        page_count=page_count,
        min_publish_date=_parse_date(published_after, "published_after"),
    )
    return _normalize_search(result)


@mcp.tool
async def get_listing(
    listing_id: Annotated[
        str,
        Field(description="Numeric Kleinanzeigen listing id, e.g. '3379172637'"),
    ],
) -> dict[str, Any]:
    """Fetch the full detail page of a single listing.

    Includes the complete description, price, seller profile, location, images
    and category path. For more than one listing prefer `get_listings_batch`,
    which shares a browser context and is markedly faster.
    """
    listing_id = listing_id.strip()
    if not listing_id:
        raise ValueError("listing_id must not be empty")

    result = await get_inserate_details_optimized(_manager(), listing_id)
    if not result.get("success"):
        if result.get("not_found"):
            return {"success": False, "status": "deleted", "id": listing_id}
        raise RuntimeError(f"Could not fetch listing {listing_id}")
    return _slim(result.get("data") or {})


@mcp.tool
async def get_listings_batch(
    listing_ids: Annotated[
        list[str],
        Field(description="Listing ids to fetch, typically taken from a search"),
    ],
    max_concurrent: Annotated[
        int,
        Field(description="Detail pages to fetch in parallel", ge=1, le=5),
    ] = 2,
) -> dict[str, Any]:
    """Fetch full details for several listings in one call.

    The normal follow-up to `search_listings`. Failed ids are reported in
    `errors` rather than failing the whole call, so a deleted listing does not
    lose you the rest. Keep `max_concurrent` low — Kleinanzeigen throttles
    aggressive parallel access.
    """
    ids = [i.strip() for i in listing_ids if i and i.strip()]
    if not ids:
        raise ValueError("listing_ids must contain at least one id")
    if len(ids) > MAX_BATCH_SIZE:
        raise ValueError(
            f"Too many ids ({len(ids)}); fetch at most {MAX_BATCH_SIZE} per call"
        )

    manager = _manager()
    semaphore = asyncio.Semaphore(max_concurrent)

    async def fetch(listing_id: str) -> dict[str, Any]:
        async with semaphore:
            return await get_inserate_details_optimized(manager, listing_id)

    outcomes = await asyncio.gather(
        *(fetch(i) for i in ids), return_exceptions=True
    )

    results: list[Any] = []
    errors: list[dict[str, str]] = []
    for listing_id, outcome in zip(ids, outcomes):
        if isinstance(outcome, BaseException):
            errors.append({"id": listing_id, "error": str(outcome)})
        elif outcome.get("success"):
            results.append(_slim(outcome.get("data")))
        elif outcome.get("not_found"):
            errors.append({"id": listing_id, "error": "listing deleted or expired"})
        else:
            errors.append({"id": listing_id, "error": "fetch failed"})

    return {
        "success": True,
        "requested": len(ids),
        "returned": len(results),
        "results": results,
        "errors": errors,
    }


@mcp.tool
async def search_by_url(
    url: Annotated[
        str,
        Field(description="A kleinanzeigen.de search or category URL"),
    ],
    max_pages: Annotated[
        int, Field(description="Result pages to fetch, ~25 listings each", ge=1)
    ] = 1,
    published_after: Annotated[
        str | None,
        Field(description="Only return listings published at or after this ISO 8601 datetime"),
    ] = None,
) -> dict[str, Any]:
    """Search using a Kleinanzeigen URL, preserving all of its filters.

    Use this when the user pastes a Kleinanzeigen search link. Category URLs
    encode filters that `search_listings` cannot express — vehicle make, model
    year, fuel type, room count and so on — and this keeps every one of them.
    Page numbers are injected automatically.
    """
    if "kleinanzeigen.de" not in url:
        raise ValueError("url must be a kleinanzeigen.de URL")

    max_pages = min(max_pages, MAX_PAGE_COUNT)
    result = await scrape_by_url(
        browser_manager=_manager(),
        base_url=url,
        max_pages=max_pages,
        min_publish_date=_parse_date(published_after, "published_after"),
    )
    return _normalize_search(result)


@mcp.tool
async def parse_search_url(
    url: Annotated[str, Field(description="A kleinanzeigen.de search or category URL")],
) -> dict[str, Any]:
    """Explain which filters a Kleinanzeigen URL encodes, without scraping it.

    Returns the filters that map onto `search_listings` arguments plus any that
    do not. Handy for telling the user what a link actually searches for, or for
    deciding whether `search_listings` is enough or `search_by_url` is required.
    This makes no network request.
    """
    parsed = parse_kleinanzeigen_url(url)
    params, unmapped = map_to_inserate_params(parsed)
    return {"search_listings_args": params, "unmapped_filters": unmapped}


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    """Container healthcheck: reports whether Chromium actually came up."""
    if _browser is None:
        return JSONResponse({"status": "starting"}, status_code=503)
    return JSONResponse({"status": "ok"})


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    transport = os.getenv("MCP_TRANSPORT", "http")
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport="http",
            host=os.getenv("MCP_HOST", "0.0.0.0"),  # noqa: S104 - containerised
            port=int(os.getenv("MCP_PORT", "8000")),
            path=os.getenv("MCP_PATH", "/mcp"),
        )


if __name__ == "__main__":
    main()
