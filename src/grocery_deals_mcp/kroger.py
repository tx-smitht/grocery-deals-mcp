"""Optional exact pricing via the official Kroger API.

Kroger's public Products API returns real SKU-level regular *and* promo
prices for a specific store, covering every Kroger banner — Kroger,
Harris Teeter, Ralphs, Fred Meyer, King Soopers, Fry's, QFC, Smith's. It
is the one sanctioned, documented feed this server uses. Everything else
works without credentials; register a free app at
https://developer.kroger.com to enable it.
"""

from __future__ import annotations

import base64
import time

import httpx

from .config import (
    HTTP_TIMEOUT,
    KROGER_CLIENT_ID,
    KROGER_CLIENT_SECRET,
    KROGER_LOCATION_ID,
)

BASE = "https://api.kroger.com/v1"
_token: dict = {"value": None, "expires_at": 0.0}


def configured() -> bool:
    return bool(KROGER_CLIENT_ID and KROGER_CLIENT_SECRET)


class KrogerError(RuntimeError):
    pass


async def _access_token(client: httpx.AsyncClient) -> str:
    if _token["value"] and time.time() < _token["expires_at"] - 60:
        return _token["value"]

    basic = base64.b64encode(
        f"{KROGER_CLIENT_ID}:{KROGER_CLIENT_SECRET}".encode()
    ).decode()
    resp = await client.post(
        f"{BASE}/connect/oauth2/token",
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials", "scope": "product.compact"},
    )
    if resp.status_code != 200:
        raise KrogerError(
            f"Kroger auth failed ({resp.status_code}). Check KROGER_CLIENT_ID / SECRET."
        )
    payload = resp.json()
    _token["value"] = payload["access_token"]
    _token["expires_at"] = time.time() + payload.get("expires_in", 1800)
    return _token["value"]


def _squash(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum())


async def find_location(zip_code: str, chain: str = "", limit: int = 10) -> list[dict]:
    """Kroger-banner store IDs near a ZIP. Needed once, for KROGER_LOCATION_ID.

    Args:
        zip_code: 5-digit US ZIP to search near.
        chain: optional banner filter. Matched against both the store name
            and Kroger's internal chain code, so "Harris Teeter", "harristeeter"
            and "HART" all work. Omit for every Kroger-family store nearby.
        limit: how many stores to return.

    Kroger's own `filter.chain` takes undocumented internal codes (Harris
    Teeter is "HART", not "HARRISTEETER") and returns an empty list rather
    than an error for an unrecognized one. Filtering client-side instead
    means a plausible-looking guess can't silently yield nothing.
    """
    params = {"filter.zipCode.near": zip_code, "filter.limit": max(limit, 25)}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        token = await _access_token(client)
        resp = await client.get(
            f"{BASE}/locations",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
    if resp.status_code != 200:
        raise KrogerError(f"Kroger locations failed ({resp.status_code}).")

    data = resp.json().get("data", [])
    if chain:
        want = _squash(chain)
        data = [
            loc for loc in data
            if want in _squash(loc.get("chain")) or want in _squash(loc.get("name"))
        ]
    return [
        {
            "location_id": loc.get("locationId"),
            "name": loc.get("name"),
            "chain": loc.get("chain"),
            "address": " ".join(
                filter(
                    None,
                    [
                        (loc.get("address") or {}).get("addressLine1"),
                        (loc.get("address") or {}).get("city"),
                        (loc.get("address") or {}).get("state"),
                    ],
                )
            ),
        }
        for loc in data[:limit]
    ]


async def search_products(
    term: str, limit: int = 15, location_id: str = ""
) -> list[dict]:
    """Products with regular + promo price at one Kroger-banner store."""
    location_id = location_id or KROGER_LOCATION_ID
    if not location_id:
        raise KrogerError(
            "No store selected. Call kroger_find_stores once to get a "
            "location_id, then pass it in or set KROGER_LOCATION_ID."
        )
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        token = await _access_token(client)
        resp = await client.get(
            f"{BASE}/products",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "filter.term": term,
                "filter.locationId": location_id,
                "filter.limit": limit,
            },
        )
    if resp.status_code != 200:
        raise KrogerError(f"Kroger product search failed ({resp.status_code}).")

    out = []
    for p in resp.json().get("data", []):
        item = (p.get("items") or [{}])[0]
        price = item.get("price") or {}
        regular, promo = price.get("regular"), price.get("promo")
        out.append(
            {
                "name": p.get("description"),
                "brand": p.get("brand"),
                "size": item.get("size"),
                "regular": regular,
                "promo": promo if promo else None,
                "on_sale": bool(promo and regular and promo < regular),
                "savings_pct": (
                    round((regular - promo) / regular * 100)
                    if promo and regular and promo < regular
                    else 0
                ),
            }
        )
    out.sort(key=lambda d: (not d["on_sale"], -d["savings_pct"]))
    return out
