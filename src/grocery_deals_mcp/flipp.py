"""Client for Flipp's weekly-circular backend.

Flipp aggregates the printed/digital weekly ads for ~2000 US retailers.
The endpoints used here are the ones its own web app calls. They are
undocumented and unauthenticated, so: cache hard, fetch rarely, and treat
a schema change as expected rather than exceptional.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from . import cache
from .config import (
    CACHE_TTL_HOURS,
    GROCERY_CATEGORY,
    HTTP_TIMEOUT,
    STORE_FILTER,
    USER_AGENT,
    store_matches,
)

BASE = "https://backflipp.wishabi.com/flipp"
_MAX_CONCURRENT_FLYERS = 6


class FlippError(RuntimeError):
    pass


async def _get(client: httpx.AsyncClient, path: str, **params: Any) -> dict:
    params.setdefault("locale", "en-us")
    try:
        resp = await client.get(f"{BASE}/{path}", params=params)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        raise FlippError(f"Flipp returned {e.response.status_code} for /{path}") from e
    except httpx.HTTPError as e:
        raise FlippError(f"Could not reach Flipp for /{path}: {e}") from e
    except ValueError as e:
        raise FlippError(f"Flipp sent non-JSON for /{path}") from e


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )


def _price(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return round(float(raw), 2)
    except (TypeError, ValueError):
        return None


def _keep_store(merchant: str) -> bool:
    if not STORE_FILTER:
        return True
    return any(store_matches(merchant, s) for s in STORE_FILTER)


# --------------------------------------------------------------------------
# Flyers
# --------------------------------------------------------------------------

async def get_flyers(
    zip_code: str,
    grocery_only: bool = True,
    force: bool = False,
) -> list[dict]:
    """Active circulars for a postal code."""
    key = f"flyers:{zip_code}"
    raw = None if force else cache.cache_get(key)
    if raw is None:
        async with _client() as client:
            raw = await _get(client, "data", postal_code=zip_code)
        cache.cache_put(key, raw)

    out = []
    for f in raw.get("flyers", []):
        merchant = (f.get("merchant") or "").strip()
        if not merchant:
            continue
        categories = f.get("categories") or []
        if grocery_only and GROCERY_CATEGORY not in categories:
            continue
        if not _keep_store(merchant):
            continue
        out.append(
            {
                "store": merchant,
                "flyer_id": f.get("id"),
                "title": f.get("name"),
                "valid_from": (f.get("valid_from") or "")[:10],
                "valid_to": (f.get("valid_to") or "")[:10],
                "categories": categories,
            }
        )
    out.sort(key=lambda x: (x["store"].lower(), x["valid_to"]))
    return out


async def get_flyer_items(flyer_id: int, store: str, force: bool = False) -> list[dict]:
    """Every advertised item in one circular."""
    key = f"flyer_items:{flyer_id}"
    raw = None if force else cache.cache_get(key)
    if raw is None:
        async with _client() as client:
            raw = await _get(client, f"flyers/{flyer_id}")
        cache.cache_put(key, raw)

    items = []
    for it in raw.get("items", []):
        name = (it.get("name") or "").strip()
        if not name:
            continue
        items.append(
            {
                "store": store,
                "name": name,
                "brand": it.get("brand"),
                "price": _price(it.get("price")),
                "unit": None,
                "sale_story": it.get("sale_story"),
                "valid_from": (it.get("valid_from") or "")[:10],
                "valid_to": (it.get("valid_to") or "")[:10],
                "flyer_id": flyer_id,
                "item_id": it.get("id"),
            }
        )
    return items


async def refresh_index(zip_code: str, force: bool = True) -> dict:
    """Pull every active grocery circular for a ZIP and rebuild its index.

    This is the only slow operation in the server (~10-20s for a dozen
    circulars). Every query tool reads the index it produces.
    """
    flyers = await get_flyers(zip_code=zip_code, force=force)
    sem = asyncio.Semaphore(_MAX_CONCURRENT_FLYERS)

    async def one(f: dict) -> list[dict]:
        async with sem:
            try:
                return await get_flyer_items(f["flyer_id"], f["store"], force=force)
            except FlippError:
                return []  # One bad circular shouldn't sink the refresh.

    batches = await asyncio.gather(*(one(f) for f in flyers))
    items = [it for batch in batches for it in batch]

    cache.record_prices(items)
    indexed = cache.replace_deals(zip_code, items)
    return {
        "zip": zip_code,
        "indexed_items": indexed,
        "flyers": len(flyers),
        "stores": cache.indexed_stores(zip_code),
    }


async def ensure_fresh(zip_code: str, ttl_hours: float | None = None) -> bool:
    """Rebuild the index only if missing or stale. True if it refreshed."""
    ttl = CACHE_TTL_HOURS if ttl_hours is None else ttl_hours
    age = cache.index_age_hours(zip_code)
    if age is not None and age < ttl:
        return False
    await refresh_index(zip_code=zip_code, force=age is not None)
    return True


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

async def search(
    query: str,
    zip_code: str,
    grocery_only: bool = True,
    force: bool = False,
) -> list[dict]:
    """Keyword search across every circular in the area.

    Complements the local index: these results carry unit ('LB', 'EA') and
    loyalty-program qualifiers ('MVP', 'with card') that flyer dumps lack.
    """
    key = f"search:{zip_code}:{query.lower().strip()}"
    raw = None if force else cache.cache_get(key)
    if raw is None:
        async with _client() as client:
            raw = await _get(client, "items/search", postal_code=zip_code, q=query)
        cache.cache_put(key, raw)

    grocery_stores = set()
    if grocery_only:
        try:
            grocery_stores = {f["store"] for f in await get_flyers(zip_code)}
        except FlippError:
            grocery_only = False

    out = []
    for it in raw.get("items", []):
        merchant = (it.get("merchant_name") or "").strip()
        name = (it.get("name") or "").strip()
        if not merchant or not name:
            continue
        if grocery_only and merchant not in grocery_stores:
            continue
        if not _keep_store(merchant):
            continue
        out.append(
            {
                "store": merchant,
                "name": name,
                "price": _price(it.get("current_price")),
                "was_price": _price(it.get("original_price")),
                "unit": it.get("post_price_text"),
                "qualifier": it.get("pre_price_text"),
                "sale_story": it.get("sale_story"),
                "category": it.get("_L2") or it.get("_L1"),
                "valid_from": (it.get("valid_from") or "")[:10],
                "valid_to": (it.get("valid_to") or "")[:10],
                "flyer_id": it.get("flyer_id"),
                "item_id": it.get("id"),
            }
        )
    cache.record_prices(out)
    return out
