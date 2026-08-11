"""Configuration. Everything location-specific comes from the caller or
the environment — nothing about any particular place is baked in here."""

from __future__ import annotations

import os
import re
from pathlib import Path

# Optional convenience default so you don't repeat your ZIP every call.
# Unset by default: tools then require an explicit zip_code argument.
DEFAULT_ZIP = os.environ.get("GROCERY_ZIP", "").strip()

# Flipp tags each circular with categories. "Groceries" is the reliable,
# location-neutral way to separate supermarkets from hardware stores and
# clothing retailers, so no store list needs hardcoding.
GROCERY_CATEGORY = "Groceries"

# Optional allowlist, e.g. GROCERY_STORES="Aldi,Lidl,Costco". Empty means
# "every store with a grocery circular in that ZIP".
_env_stores = os.environ.get("GROCERY_STORES", "").strip()
STORE_FILTER = [s.strip() for s in _env_stores.split(",") if s.strip()]

# Circulars turn over weekly, so half a day is plenty fresh.
CACHE_TTL_HOURS = float(os.environ.get("GROCERY_CACHE_TTL_HOURS", "12"))

DB_PATH = Path(
    os.environ.get(
        "GROCERY_DB_PATH",
        Path(__file__).resolve().parents[2] / "data" / "deals.db",
    )
).expanduser()

# Optional: Kroger API credentials unlock exact Kroger-banner pricing
# (Kroger, Harris Teeter, Ralphs, Fred Meyer, King Soopers, ...).
KROGER_CLIENT_ID = os.environ.get("KROGER_CLIENT_ID", "")
KROGER_CLIENT_SECRET = os.environ.get("KROGER_CLIENT_SECRET", "")
KROGER_LOCATION_ID = os.environ.get("KROGER_LOCATION_ID", "")

HTTP_TIMEOUT = float(os.environ.get("GROCERY_HTTP_TIMEOUT", "30"))
USER_AGENT = os.environ.get(
    "GROCERY_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) grocery-deals-mcp/0.1",
)

_ZIP_RE = re.compile(r"^\d{5}$")


class ConfigError(ValueError):
    pass


def resolve_zip(zip_code: str | None) -> str:
    """Pick the ZIP for a request: explicit argument, else the env default."""
    candidate = (zip_code or "").strip() or DEFAULT_ZIP
    if not candidate:
        raise ConfigError(
            "No ZIP code given. Pass zip_code to the tool (for example "
            '"12345"), or set GROCERY_ZIP in the server environment.'
        )
    if not _ZIP_RE.match(candidate):
        raise ConfigError(f"'{candidate}' is not a 5-digit US ZIP code.")
    return candidate


def store_matches(merchant: str, wanted: str) -> bool:
    """Loose merchant-name comparison.

    Flipp is inconsistent about trailing spaces and apostrophes
    ("Costco ", "Wegman's", "Sam's Club"), so compare on a squashed form.
    """
    def norm(s: str) -> str:
        return "".join(c for c in s.lower() if c.isalnum())

    a, b = norm(merchant), norm(wanted)
    return a == b or a.startswith(b) or b.startswith(a)
