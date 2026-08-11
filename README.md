# grocery-deals-mcp

An MCP server that tells an AI assistant what's actually on sale at grocery
stores near any US ZIP code this week.

It reads the weekly circulars that supermarkets publish — the same ads that
used to arrive in the newspaper — and turns them into something an agent can
query: *"what protein is cheap this week?"*, *"where should I buy this list?"*,
*"is $1.99/lb a good price for chicken, or should I wait?"*

Typical coverage for a suburban ZIP is 15–25 grocery chains and several
thousand advertised items, including Aldi, Costco, Food Lion, Giant, Harris
Teeter, H Mart, Lidl, Publix, Safeway, Sprouts, Target, Walmart, Wegmans and
Weis, depending on what operates in your area.

## Why it's fast

Every query tool reads a local SQLite index and never touches the network.
Only `refresh_deals` goes out to fetch circulars (10–20 seconds), and it
self-heals — the first query of the week builds the index, and every call after
that returns in milliseconds until the circulars turn over.

## Tools

| Tool | What it does |
| --- | --- |
| `list_stores` | Which stores near a ZIP have an active ad, and how many deals each is running |
| `search_deals` | Find a product across every local store's ad, cheapest first |
| `store_circular` | Dump one store's entire weekly ad |
| `shopping_plan` | Turn a shopping list into a per-store trip plan, capped at N stores |
| `price_check` | Judge a price against the history this server accumulates |
| `refresh_deals` | Force an index rebuild mid-week |
| `kroger_search` | Exact SKU-level regular/promo prices (optional, see below) |
| `kroger_find_stores` | Look up a Kroger-banner store ID (optional) |

## Setup

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/tx-smitht/grocery-deals-mcp.git
cd grocery-deals-mcp && uv sync
```

Verify it starts:

```bash
uv run grocery-deals-mcp
```

It will sit silently waiting for MCP traffic on stdin — that's correct. Ctrl-C
to exit.

## Setting your location

**The agent passes the ZIP code.** Every location-aware tool takes a
`zip_code` argument, so you can just say *"what's on sale near 12345?"* and it
works with no configuration at all. Nothing about any location is stored in
this repository.

If you'd rather not repeat your ZIP every conversation, set `GROCERY_ZIP` in
your client config (below) and the argument becomes optional. Note that this
writes your ZIP into a local config file — keep that file out of version
control. `.mcp.json` and `.env` are already in `.gitignore`.

### All configuration

Every setting is an environment variable, all optional:

| Variable | Default | Purpose |
| --- | --- | --- |
| `GROCERY_ZIP` | *(none)* | Default ZIP, so tools don't need the argument |
| `GROCERY_STORES` | *(all)* | Comma-separated allowlist, e.g. `Aldi,Lidl,Costco` |
| `GROCERY_CACHE_TTL_HOURS` | `12` | How long the index stays fresh |
| `GROCERY_DB_PATH` | `./data/deals.db` | Where the cache and price history live |
| `KROGER_CLIENT_ID` / `KROGER_CLIENT_SECRET` | *(none)* | Enables `kroger_search` |
| `KROGER_LOCATION_ID` | *(none)* | Default Kroger-banner store |

To change what counts as a grocery store, edit `GROCERY_CATEGORY` in
[`src/grocery_deals_mcp/config.py`](src/grocery_deals_mcp/config.py) — Flipp
also tags circulars as `Pharmacy`, `Pets`, `Home & Garden`, `Electronics` and
others, so the same code will happily index those instead.

## Connecting it

### Claude Code

One command, from anywhere:

```bash
claude mcp add grocery-deals -- uv run --directory /absolute/path/to/grocery-deals-mcp grocery-deals-mcp
```

With a default ZIP baked in:

```bash
claude mcp add grocery-deals --env GROCERY_ZIP=12345 -- uv run --directory /absolute/path/to/grocery-deals-mcp grocery-deals-mcp
```

Then `/mcp` inside Claude Code to confirm it connected.

### Claude Desktop

Edit `claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "grocery-deals": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/grocery-deals-mcp", "grocery-deals-mcp"],
      "env": { "GROCERY_ZIP": "12345" }
    }
  }
}
```

Restart Claude Desktop. Drop the `env` block to pass ZIPs per-question instead.

### Codex CLI

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.grocery-deals]
command = "uv"
args = ["run", "--directory", "/absolute/path/to/grocery-deals-mcp", "grocery-deals-mcp"]
env = { GROCERY_ZIP = "12345" }
```

### Anything else

It's a standard stdio MCP server. Any client that speaks MCP can run
`uv run --directory <path> grocery-deals-mcp`.

A copyable starting point is in
[`.mcp.json.example`](.mcp.json.example).

## Optional: exact Kroger-banner pricing

The circular tells you *"Boneless Chicken Breast $1.99/lb"*. Kroger's official
API tells you the exact SKU, size, regular price and promo price at one
specific store. It covers every Kroger banner — Kroger, Harris Teeter, Ralphs,
Fred Meyer, King Soopers, QFC, Smith's, Fry's.

1. Register a free app at [developer.kroger.com](https://developer.kroger.com)
   and request the **Products** API
2. Set `KROGER_CLIENT_ID` and `KROGER_CLIENT_SECRET`
3. Ask the agent to run `kroger_find_stores`, then set `KROGER_LOCATION_ID` to
   the store you use (or pass `location_id` per call)

`kroger_find_stores` accepts everyday chain names — "Harris Teeter" works, and
so does Kroger's internal code for it (`HART`). Matching happens client-side
because Kroger's own `filter.chain` takes undocumented codes and answers an
unrecognized one with an empty list rather than an error.

Free tier allows 10,000 calls/day. Everything else in this server works
without it.

## How it works, and what that implies

Circular data comes from Flipp's backend — the service behind the weekly-ad app
used by most large US grocers. **These endpoints are undocumented and
unofficial.** They require no key and are the same calls Flipp's own web app
makes, but there is no API contract behind them: they can change shape or
disappear without notice, and this project isn't affiliated with or endorsed by
Flipp or any retailer.

Accordingly, the server caches aggressively and fetches rarely — one refresh
per ZIP per 12 hours by default, six concurrent requests at most. Please don't
lower those defaults to hammer someone else's servers.

Some real limits worth knowing:

- **Trader Joe's will never appear.** They don't run weekly sales or publish a
  circular. That's a fact about Trader Joe's, not a gap in the data.
- **Prices are what the ad claims.** Loyalty-card qualifiers (`MVP`, `with
  card`), digital-coupon requirements and quantity limits are captured when the
  ad states them, but the ad is the source of truth, not the register.
- **Flyer dumps lack units.** A store's full circular gives names and prices;
  `search_deals` falls back to Flipp's search index, which adds `LB`/`EA` units
  and loyalty qualifiers, when a term is thin locally.
- **A ZIP's feed can include chains that aren't local.** Flipp's radius is
  generous. Use `GROCERY_STORES` to pin it down to stores you'd actually drive
  to.

## Privacy

Nothing leaves your machine except circular requests to Flipp (a ZIP code and
search terms) and, if you enable it, product lookups to Kroger.

The SQLite database records your ZIP and accumulated price history. It lives in
`data/` and is gitignored. So is `.env` and `.mcp.json` — if you set a default
ZIP, that's where it should go.

## License

MIT
