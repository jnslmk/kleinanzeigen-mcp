#!/usr/bin/env python3
"""Run against a deployed MCP: python scripts/check-price-filters.py ENDPOINT."""

import asyncio
import re
import sys
from decimal import Decimal

from fastmcp import Client


async def main(endpoint: str) -> None:
    cases = [
        ("range", {"min_price": "80", "max_price": "300", "page_count": 2}),
        ("maximum", {"max_price": 300}),
        ("minimum", {"min_price": 80}),
    ]
    async with Client(endpoint) as client:
        for label, filters in cases:
            response = await client.call_tool(
                "search_listings", {"query": "Intel NUC", **filters}
            )
            data = response.data
            assert data["success"], f"{label}: search failed: {data}"
            listings = data["results"]
            assert listings, (
                f"{label}: price-filtered search silently returned no listings"
            )
            prices = []
            for listing in listings:
                match = re.search(r"\d[\d.,]*", str(listing["price"]))
                if match is None:
                    continue  # Negotiable/unspecified prices have no numeric bound.
                price = Decimal(match.group().replace(".", "").replace(",", "."))
                if "min_price" in filters:
                    assert price >= Decimal(str(filters["min_price"])), listing
                if "max_price" in filters:
                    assert price <= Decimal(str(filters["max_price"])), listing
                prices.append(price)
            assert prices, f"{label}: no usable prices"
            print(
                f"{label}: {len(listings)} listings; numeric prices EUR {min(prices)}–{max(prices)}",
                flush=True,
            )


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
