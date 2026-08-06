"""MCP server exposing Kleinanzeigen.de listing search as tools.

The scraping itself is done by DanielWTE/ebay-kleinanzeigen-api, which is baked
into the image at a pinned commit and imported directly (see the Dockerfile).
Upstream ships a FastAPI app; we skip it entirely and drive the scraper
functions ourselves, so there is no second process and no HTTP hop.

Upstream's scrapers take an ``OptimizedPlaywrightManager`` as an explicit first
argument, so the only things this module has to own are that manager's
lifecycle and the process-wide gate that queues concurrent tool calls.
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
from playwright.async_api import BrowserContext
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

# Upstream (DanielWTE/ebay-kleinanzeigen-api) — importable because the image
# puts its checkout on PYTHONPATH. Upstream is not a installable package; these
# are its top-level modules.
from scrapers.inserat import get_inserate_details_optimized
from scrapers.inserate_by_url import scrape_by_url
from scrapers.inserate_ultra_optimized import ultra_optimized_scrape_inserate
from utils.browser import OptimizedPlaywrightManager, get_random_ua
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

# MAX_CONCURRENT caps how many MCP tool calls may scrape Kleinanzeigen at once,
# process-wide. Chat agents (LibreChat) fire 4-8 calls in a burst, which trips
# Kleinanzeigen's bot detection; the gate queues the excess calls instead of
# scraping them in parallel.
MAX_CONCURRENT = int(os.getenv("KZ_MAX_CONCURRENT", "2"))

# Search responses are fed straight into an LLM context window, so page_count is
# capped well below upstream's limit of 20 (~25 listings per page).
MAX_PAGE_COUNT = int(os.getenv("KZ_MAX_PAGE_COUNT", "5"))

# Detail fetches open a page each; more than a few in parallel trips
# Kleinanzeigen's bot detection.
MAX_BATCH_SIZE = int(os.getenv("KZ_MAX_BATCH_SIZE", "20"))

_browser: SafePlaywrightManager | None = None
_scrape_gate: asyncio.Semaphore | None = None


def _manager() -> SafePlaywrightManager:
    if _browser is None:  # pragma: no cover - guarded by the lifespan
        raise RuntimeError("Browser manager is not running")
    return _browser


def _gate() -> asyncio.Semaphore:
    if _scrape_gate is None:  # pragma: no cover - guarded by the lifespan
        raise RuntimeError("Scrape gate is not running")
    return _scrape_gate


class SafePlaywrightManager(OptimizedPlaywrightManager):
    """Context pool that cannot deadlock or leak contexts.

    Upstream's ``get_context`` recurses into itself while still holding the
    pool lock when every context is busy — ``asyncio.Lock`` is not reentrant,
    so that recursion deadlocks the whole pool. Its ``release_context`` also
    does slow I/O (``page.close``, ``clear_cookies``) under the same lock, so
    any release blocked on the deadlock leaks its context out of the pool
    permanently. LibreChat agents fire several MCP calls in parallel, which
    exhausts the 4-context pool and reliably trips both bugs: every request —
    parallel or not — then hangs until FastMCP kills it at its 180 s deadline,
    and only a container restart recovers the pool.

    Both methods are replaced here: waiters wait on an ``asyncio.Condition``
    (the lock is released while waiting, so it cannot self-deadlock) and a
    context is returned to the pool before its pages are cleaned up, so a
    cancelled or slow cleanup can never leak it or wedge the pool.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pool_condition = asyncio.Condition()

    async def get_context(self) -> BrowserContext:
        while True:
            async with self._pool_condition:
                if self._context_pool:
                    context = self._context_pool.pop()
                    self._context_in_use.append(context)
                    self._contexts_reused += 1
                    return context
                if len(self._context_in_use) < self._max_contexts:
                    context = await self._browser.new_context(
                        user_agent=get_random_ua()
                    )
                    self._context_in_use.append(context)
                    self._contexts_created += 1
                    return context
                # Every context is busy: wait for a release. Condition.wait()
                # releases the lock, so concurrent releases can still run, and
                # the wait is cancellable (FastMCP's request deadline).
                try:
                    await asyncio.wait_for(self._pool_condition.wait(), timeout=120)
                except TimeoutError as exc:
                    raise RuntimeError(
                        "All browser contexts are busy and none freed up within "
                        "120 s — Kleinanzeigen may be throttling this server"
                    ) from exc

    async def release_context(self, context: BrowserContext) -> None:
        # Return the context to the pool before touching its pages, so a slow
        # or cancelled cleanup can never leak it or block the pool.
        keep = False
        async with self._pool_condition:
            if context not in self._context_in_use:
                return
            self._context_in_use.remove(context)
            keep = len(self._context_pool) < self._max_contexts // 2
            if keep:
                self._context_pool.append(context)
            # Snapshot the pages BEFORE waking waiters: a waiter that pops
            # this context can open a new page immediately, and the cleanup
            # below must only close pages that existed at release time.
            pages = list(context.pages)
            self._pool_condition.notify_all()
        # Best-effort cleanup outside the lock.
        try:
            if keep:
                for page in pages:
                    await page.close()
                await context.clear_cookies()
            else:
                await context.close()
        except BaseException:  # cleanup must never raise
            pass


@asynccontextmanager
async def lifespan(_: FastMCP) -> AsyncIterator[None]:
    """Start one shared Chromium instance for the process lifetime."""
    global _browser, _scrape_gate
    _browser = SafePlaywrightManager(
        max_contexts=MAX_CONTEXTS, max_concurrent=MAX_CONCURRENT
    )
    await _browser.start()
    _scrape_gate = asyncio.Semaphore(MAX_CONCURRENT)
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
        _scrape_gate = None


mcp = FastMCP(
    name="kleinanzeigen",
    version="0.1.2",
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


def _coerce_int(
    value: str | int | None, field: str, *, ge: int | None = None
) -> int | None:
    """Coerce the numeric strings LLMs routinely send for int parameters.

    FastMCP validates tool input against the JSON schema before the function
    runs, so a parameter typed ``int`` rejects the string ``"600"`` outright
    (observed repeatedly for ``max_price``). Accepting ``str | int`` in the
    schema and normalising here keeps the model-facing contract lenient while
    the scraper still sees a real integer.
    """
    if value is None or isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.strip():
        try:
            result = int(value.strip())
        except ValueError as exc:
            raise ValueError(f"{field} must be an integer, got {value!r}") from exc
    else:
        raise ValueError(f"{field} must be an integer, got {value!r}")
    if ge is not None and result is not None and result < ge:
        raise ValueError(f"{field} must be >= {ge}, got {result}")
    return result


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
        str | int | None,
        Field(description="Search radius around `location` in kilometres"),
    ] = None,
    min_price: Annotated[
        str | int | None, Field(description="Minimum price in EUR")
    ] = None,
    max_price: Annotated[
        str | int | None, Field(description="Maximum price in EUR")
    ] = None,
    page_count: Annotated[
        str | int,
        Field(description="Result pages to fetch, ~25 listings each"),
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
    radius_km = _coerce_int(radius_km, "radius_km", ge=0)
    min_price = _coerce_int(min_price, "min_price", ge=0)
    max_price = _coerce_int(max_price, "max_price", ge=0)
    page_count = min(_coerce_int(page_count, "page_count", ge=1) or 1, MAX_PAGE_COUNT)
    min_publish_date = _parse_date(published_after, "published_after")
    async with _gate():
        result = await ultra_optimized_scrape_inserate(
            browser_manager=_manager(),
            query=query,
            location=location,
            radius=radius_km,
            min_price=min_price,
            max_price=max_price,
            page_count=page_count,
            min_publish_date=min_publish_date,
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

    async with _gate():
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
        str | int,
        Field(description="Detail pages to fetch in parallel"),
    ] = 2,
) -> dict[str, Any]:
    """Fetch full details for several listings in one call.

    The normal follow-up to `search_listings`. Failed ids are reported in
    `errors` rather than failing the whole call, so a deleted listing does not
    lose you the rest. The process-wide scrape gate (KZ_MAX_CONCURRENT) caps
    parallel tool calls; `max_concurrent` only limits the detail fetches
    inside this call.
    """
    ids = [i.strip() for i in listing_ids if i and i.strip()]
    if not ids:
        raise ValueError("listing_ids must contain at least one id")
    if len(ids) > MAX_BATCH_SIZE:
        raise ValueError(
            f"Too many ids ({len(ids)}); fetch at most {MAX_BATCH_SIZE} per call"
        )
    max_concurrent = min(_coerce_int(max_concurrent, "max_concurrent", ge=1) or 2, 5)

    manager = _manager()
    async with _gate():
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
        str | int, Field(description="Result pages to fetch, ~25 listings each")
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

    max_pages = min(_coerce_int(max_pages, "max_pages", ge=1) or 1, MAX_PAGE_COUNT)
    min_publish_date = _parse_date(published_after, "published_after")
    async with _gate():
        result = await scrape_by_url(
            browser_manager=_manager(),
            base_url=url,
            max_pages=max_pages,
            min_publish_date=min_publish_date,
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
