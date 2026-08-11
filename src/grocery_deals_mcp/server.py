"""MCP server: what's on sale at grocery stores near any US ZIP code.

Design note on speed — every query tool reads a local SQLite index and
never touches the network. The index is rebuilt by `refresh_deals`, the
one slow call (~10-20s). It self-heals: if a ZIP's index is missing or
older than the TTL, the next query rebuilds it once, and every call after
that is instant for the rest of the week.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from collections import defaultdict

# stdio transport speaks JSON-RPC on stdout; a stray httpx INFO line during a
# refresh would corrupt the stream.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from . import cache, flipp, kroger
from .config import CACHE_TTL_HOURS, ConfigError, resolve_zip

mcp = _Server("grocery-deals")

# One refresh at a time per ZIP: several tools can trip a stale index at
# once, and rebuilding the same circulars concurrently helps nobody.
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


async def _ready(zip_code: str) -> str:
    """Guarantee a usable index; return a note if we had to build one."""
    age = cache.index_age_hours(zip_code)
    if age is not None and age < CACHE_TTL_HOURS:
        return ""
    async with _locks[zip_code]:
        age = cache.index_age_hours(zip_code)  # May have been built while waiting.
        if age is not None and age < CACHE_TTL_HOURS:
            return ""
        result = await flipp.refresh_index(zip_code, force=age is not None)
    return f"_(built index for {zip_code}: {result['indexed_items']} deals)_\n\n"


def _fmt(d: dict, show_store: bool = True) -> str:
    bits = []
    if show_store:
        bits.append(f"**{d['store'].strip()}**")
    bits.append(d["name"])
    if d.get("price") is not None:
        price = f"${d['price']:.2f}"
        if d.get("unit"):
            price += f"/{d['unit'].lower()}"
        if d.get("qualifier"):
            price += f" ({d['qualifier']})"
        bits.append(price)
    else:
        bits.append("_price in ad_")
    if d.get("valid_to"):
        bits.append(f"thru {d['valid_to'][5:]}")
    return " — ".join(bits)


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

@mcp.tool()
async def list_stores(zip_code: str = "") -> str:
    """List grocery stores near a ZIP code that have an active weekly ad,
    with how many deals each is running. Start here.

    Args:
        zip_code: 5-digit US ZIP code, e.g. "12345". Optional if the server
            has a GROCERY_ZIP default configured.
    """
    try:
        zip_code = resolve_zip(zip_code)
    except ConfigError as e:
        return str(e)

    try:
        note = await _ready(zip_code)
    except flipp.FlippError as e:
        return f"Could not load circulars: {e}"

    stores = cache.indexed_stores(zip_code)
    if not stores:
        return f"No active grocery circulars found for {zip_code}."

    lines = [f"Active grocery circulars near {zip_code}:", ""]
    for s in stores:
        lines.append(
            f"- **{s['store'].strip()}** — {s['items']} deals "
            f"({s['priced']} with prices), valid {s['valid_from']} to {s['valid_to']}"
        )
    lines += [
        "",
        f"{sum(s['items'] for s in stores)} advertised items total.",
        "_Stores that publish no weekly circular (Trader Joe's, most warehouse "
        "clubs' in-club-only offers) will not appear._",
    ]
    return note + "\n".join(lines)


@mcp.tool()
async def search_deals(
    query: str,
    zip_code: str = "",
    stores: list[str] | None = None,
    max_price: float | None = None,
    limit: int = 25,
) -> str:
    """Search this week's ads across every local grocery store for a product.

    Args:
        query: what you're looking for, e.g. "chicken breast", "greek yogurt".
        zip_code: 5-digit US ZIP code. Optional if GROCERY_ZIP is set.
        stores: optional store names to restrict to, e.g. ["Aldi", "Costco"].
        max_price: only return items at or below this price.
        limit: max results (default 25).
    """
    try:
        zip_code = resolve_zip(zip_code)
    except ConfigError as e:
        return str(e)

    try:
        note = await _ready(zip_code)
    except flipp.FlippError as e:
        return f"Could not load circulars: {e}"

    hits = cache.search_index(
        zip_code, query=query, stores=stores, max_price=max_price, limit=limit
    )

    # Circular dumps carry no unit or loyalty qualifier. When the local
    # index is thin on a term, Flipp's own search has both, so fall through.
    if len(hits) < 3:
        try:
            live = await flipp.search(query, zip_code)
            seen = {(h["store"], h["name"]) for h in hits}
            for it in live:
                if (it["store"], it["name"]) not in seen:
                    hits.append(it)
            hits = hits[:limit]
        except flipp.FlippError:
            pass

    if not hits:
        return note + f"Nothing advertised for '{query}' near {zip_code} this week."

    lines = [f"**{query}** — {len(hits)} deals near {zip_code}:", ""]
    lines += [f"- {_fmt(h)}" for h in hits]

    priced = [h for h in hits if h.get("price") is not None]
    if len(priced) > 1:
        best = min(priced, key=lambda d: d["price"])
        lines += ["", f"Cheapest: ${best['price']:.2f} at {best['store'].strip()}."]
    return note + "\n".join(lines)


@mcp.tool()
async def store_circular(store: str, zip_code: str = "", limit: int = 150) -> str:
    """Show everything in one store's current weekly ad.

    Args:
        store: store name, e.g. "Giant Food", "Aldi", "Costco".
        zip_code: 5-digit US ZIP code. Optional if GROCERY_ZIP is set.
        limit: max items to return (default 150).
    """
    try:
        zip_code = resolve_zip(zip_code)
    except ConfigError as e:
        return str(e)

    try:
        note = await _ready(zip_code)
    except flipp.FlippError as e:
        return f"Could not load circulars: {e}"

    hits = cache.search_index(zip_code, query="", stores=[store], limit=limit)
    if not hits:
        known = ", ".join(s["store"].strip() for s in cache.indexed_stores(zip_code))
        return note + (
            f"No active ad for '{store}' near {zip_code}."
            + (f" Stores with ads: {known}" if known else "")
        )

    header = f"**{hits[0]['store'].strip()}** — {len(hits)} advertised items"
    if hits[0].get("valid_to"):
        header += f", valid thru {hits[0]['valid_to']}"
    return note + "\n".join([header, ""] + [f"- {_fmt(h, False)}" for h in hits])


@mcp.tool()
async def shopping_plan(
    items: list[str], zip_code: str = "", max_stores: int = 2
) -> str:
    """Turn a shopping list into a trip plan: which store to buy each item at.

    Args:
        items: things you want to buy, e.g. ["chicken thighs", "blueberries"].
        zip_code: 5-digit US ZIP code. Optional if GROCERY_ZIP is set.
        max_stores: how many stores you're willing to visit (default 2).
    """
    try:
        zip_code = resolve_zip(zip_code)
    except ConfigError as e:
        return str(e)
    if not items:
        return "Give me a shopping list."

    try:
        note = await _ready(zip_code)
    except flipp.FlippError as e:
        return f"Could not load circulars: {e}"

    matches = {
        want: cache.search_index(zip_code, query=want, priced_only=True, limit=12)
        for want in items
    }

    # Score stores by how much of the list each covers cheaply, then take the
    # top `max_stores`. Pure per-item optimization sends you to six stores to
    # save eleven dollars, which is not a plan anyone follows.
    store_scores: dict[str, float] = defaultdict(float)
    for hits in matches.values():
        if not hits:
            continue
        cheapest = min(h["price"] for h in hits)
        for h in hits:
            if h["price"]:
                store_scores[h["store"]] += cheapest / h["price"]

    chosen = {s for s, _ in sorted(store_scores.items(), key=lambda x: -x[1])[:max_stores]}

    plan: dict[str, list[str]] = defaultdict(list)
    unmatched, total = [], 0.0
    for want, hits in matches.items():
        pool = [h for h in hits if h["store"] in chosen] or hits
        if not pool:
            unmatched.append(want)
            continue
        best = min(pool, key=lambda d: d["price"])
        total += best["price"]
        unit = f"/{best['unit'].lower()}" if best.get("unit") else ""
        plan[best["store"]].append(
            f"{want}: {best['name']} — ${best['price']:.2f}{unit}"
        )

    lines = [f"**Shopping plan** near {zip_code} — {len(items)} items, "
             f"up to {max_stores} stores", ""]
    for store, rows in plan.items():
        lines.append(f"**{store.strip()}**")
        lines += [f"  - {r}" for r in rows]
        lines.append("")

    lines.append(f"Advertised total: ~${total:.2f}")
    if unmatched:
        lines.append("Not on sale anywhere this week: " + ", ".join(unmatched))
    return note + "\n".join(lines)


@mcp.tool()
async def price_check(item: str, zip_code: str = "") -> str:
    """Judge whether a current price is actually good, using the price history
    this server accumulates. Gets more useful the longer it has been running.

    Args:
        item: the product to evaluate, e.g. "boneless chicken breast".
        zip_code: 5-digit US ZIP code. Optional if GROCERY_ZIP is set.
    """
    try:
        zip_code = resolve_zip(zip_code)
    except ConfigError as e:
        return str(e)

    try:
        note = await _ready(zip_code)
    except flipp.FlippError as e:
        return f"Could not load circulars: {e}"

    hist = cache.history_for(item)
    priced = [h["price"] for h in hist if h.get("price") is not None]
    current = cache.search_index(zip_code, query=item, priced_only=True, limit=5)

    lines = [f"**Price check: {item}**", ""]
    if current:
        lines.append("This week:")
        lines += [f"- {_fmt(c)}" for c in current]
        lines.append("")

    if len(priced) < 4:
        lines.append(
            f"Only {len(priced)} historical price points so far — not enough to "
            "call it. History builds every time the index refreshes."
        )
        return note + "\n".join(lines)

    low, high, med = min(priced), max(priced), statistics.median(priced)
    lines.append(
        f"History: {len(priced)} points — low ${low:.2f}, "
        f"median ${med:.2f}, high ${high:.2f}"
    )
    if current:
        # Judge the best price available, not the closest text match.
        now = min(c["price"] for c in current)
        cheapest = min(current, key=lambda c: c["price"])
        lines.append(f"Best this week: {cheapest['name']} at {cheapest['store'].strip()}.")
        if now <= low * 1.02:
            verdict = "as low as I've ever recorded — stock up"
        elif now <= med * 0.9:
            verdict = "well below the usual sale price — good buy"
        elif now <= med:
            verdict = "a normal sale price"
        else:
            verdict = "above the typical sale price — wait if you can"
        lines.append(f"At ${now:.2f}, this is {verdict}.")
    return note + "\n".join(lines)


@mcp.tool()
async def refresh_deals(zip_code: str = "") -> str:
    """Force a rebuild of the deal index from Flipp. Takes 10-20 seconds.
    Only needed mid-week; the index otherwise refreshes itself.

    Args:
        zip_code: 5-digit US ZIP code. Optional if GROCERY_ZIP is set.
    """
    try:
        zip_code = resolve_zip(zip_code)
    except ConfigError as e:
        return str(e)

    async with _locks[zip_code]:
        try:
            result = await flipp.refresh_index(zip_code, force=True)
        except flipp.FlippError as e:
            return f"Refresh failed: {e}"

    stats = cache.history_stats()
    lines = [
        f"Indexed {result['indexed_items']} deals from {result['flyers']} "
        f"circulars near {zip_code}.",
        "",
    ]
    lines += [
        f"- {s['store'].strip()}: {s['items']} items thru {s['valid_to']}"
        for s in result["stores"]
    ]
    lines += [
        "",
        f"Price history: {stats['points']} points across "
        f"{stats['items']} distinct items.",
    ]
    return "\n".join(lines)


@mcp.tool()
async def kroger_search(query: str, location_id: str = "", limit: int = 15) -> str:
    """Exact SKU-level prices (regular and promo) from a Kroger-banner store
    via Kroger's official API — Kroger, Harris Teeter, Ralphs, Fred Meyer,
    King Soopers, QFC, Smith's. More precise than the circular. Requires
    Kroger API credentials; without them the other tools still work.

    Args:
        query: product term, e.g. "greek yogurt".
        location_id: store ID from kroger_find_stores. Optional if
            KROGER_LOCATION_ID is set.
        limit: max products (default 15).
    """
    if not kroger.configured():
        return (
            "Kroger API credentials are not configured, so exact pricing is "
            "unavailable — the circular-based tools still work. To enable: "
            "register a free app at https://developer.kroger.com and set "
            "KROGER_CLIENT_ID and KROGER_CLIENT_SECRET in the server env."
        )
    try:
        products = await kroger.search_products(query, limit=limit, location_id=location_id)
    except kroger.KrogerError as e:
        return str(e)
    if not products:
        return f"No products found for '{query}'."

    lines = [f"**{query}** — exact store pricing", ""]
    for p in products:
        if p["on_sale"]:
            price = f"${p['promo']:.2f} (was ${p['regular']:.2f}, -{p['savings_pct']}%)"
        elif p["regular"]:
            price = f"${p['regular']:.2f}"
        else:
            price = "no price"
        size = f" [{p['size']}]" if p.get("size") else ""
        lines.append(f"- {p['name']}{size} — {price}")
    return "\n".join(lines)


@mcp.tool()
async def kroger_find_stores(zip_code: str = "", chain: str = "") -> str:
    """Find nearby Kroger-banner store IDs, needed once to use kroger_search.

    Args:
        zip_code: 5-digit US ZIP code. Optional if GROCERY_ZIP is set.
        chain: optional banner filter, e.g. "Harris Teeter", "Kroger",
            "Ralphs". Matched against store name and chain code, so the
            everyday spelling works.
    """
    try:
        zip_code = resolve_zip(zip_code)
    except ConfigError as e:
        return str(e)
    if not kroger.configured():
        return "Kroger API credentials are not configured."
    try:
        locs = await kroger.find_location(zip_code, chain=chain)
    except kroger.KrogerError as e:
        return str(e)
    if not locs:
        return f"No Kroger-banner stores found near {zip_code}."
    return "\n".join(
        f"- `{l['location_id']}` — {l['name']} ({l['chain']}), {l['address']}"
        for l in locs
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
