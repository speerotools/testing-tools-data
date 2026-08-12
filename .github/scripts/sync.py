#!/usr/bin/env python3
"""
Airtable → testing-tools.json sync.

Emits the exact vendor shape consumed by embed.js (the mockup contract):
each vendor is { slug, name, status, acquiredBy?, h1, h2, ucf[], mcp,
mcpDetail{}, ai[], pricing[], sdk[], integrations[], warehouse[],
compliance[], url, summary }. Taxonomy is built at runtime by the embed,
so it is NOT written here. No logos.

Run locally:
    AIRTABLE_TOKEN=pat... python sync.py

Environment variables:
    AIRTABLE_TOKEN          required — PAT with data.records:read + schema.bases:read
    AIRTABLE_BASE_ID        default: appRX3rtuXifUnvD4
    AIRTABLE_DATABASE_TBL   default: tblOx4tapKq2a0sBR
    AIRTABLE_MASTER_TBL     default: tbl6FWyYcBfcIq64T
    OUTPUT_DIR              default: ./  (writes testing-tools.json here)
"""

from __future__ import annotations
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

try:
    from pyairtable import Api
except ImportError:
    print("ERROR: pyairtable not installed. Run: pip install pyairtable", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

BASE_ID   = os.environ.get("AIRTABLE_BASE_ID",     "appRX3rtuXifUnvD4")
TABLE_ID  = os.environ.get("AIRTABLE_DATABASE_TBL", "tblOx4tapKq2a0sBR")
MASTER_ID = os.environ.get("AIRTABLE_MASTER_TBL",   "tbl6FWyYcBfcIq64T")

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "."))

TOKEN = os.environ.get("AIRTABLE_TOKEN")
if not TOKEN:
    print("ERROR: AIRTABLE_TOKEN env var required", file=sys.stderr)
    sys.exit(1)

# Field IDs on the Database table (match the production Airtable schema).
# Run the schema dump once to verify: python sync.py --dump-schema
F = {
    "name":           "fldNRhgSZsb65ck0T",
    "publish":        "fldCZbJmCuVdplDQL",
    "url":            "fldOkIjb4Sx4K9XjM",
    "h1":             "fldyiQjogty7Py1Ni",
    "h2":             "fldyIlJXzq8NS9tFw",
    "summary":        "fldyOigVgycjOjbT0",
    "slug":           "fldgS3sCcrO9pY1UJ",
    "status":         "fldc3unFlsIJgEVcq",
    "ucf":            "fldzBCpS6hbGF3Se4",
    "ai":             "fld0EUOcnmfRcURYm",
    "mcp_type":       "fldv2diDTNPtVWPXX",
    "mcp_server_url": "fldEgRFMwP2kJad1u",
    "mcp_hosted":     "fldw6HE3s1EP1OxcR",
    "mcp_docs":       "fldEOG9f8iDoGVlfp",
    "pricing":        "fld9zIM00jFToKV6M",
    "sdk":            "fldS4GbYUELK4ehLd",
    "integrations":   "flddtHBBRMo3crCgq",
    "warehouse":      "fldhkZMh3r4lfnZIk",
    "compliance":     "fldFjURaYhZWlwHmm",
    "acquired_by":    "fldLrwKE4NqkVYlu0",
    # Quadrant map positions (Number fields). Agentic X/Y Final is the position
    # the embed plots (Airtable already encodes override-beats-computed there);
    # Computed + Override ride along so the page can show drift. Market overrides
    # win over the JS-computed market position for those vendors.
    "ax":  "fldgy3uob0gVIyLP8",  # Agentic Map X Final
    "ay":  "flde9uayklPmxqNny",  # Agentic Map Y Final
    "axc": "fldMR1dUGg6yRJRYe",  # Agentic Map X Computed
    "ayc": "fldCG6yjX27k881w4",  # Agentic Map Y Computed
    "axo": "fldOFGtBuyoj9JI9F",  # Agentic Map X Override
    "ayo": "fldNamypy2pKrRbzD",  # Agentic Map Y Override
    "mxo": "fldW3nEUbQRmmjXSm",  # Market Map X Override
    "myo": "fldOGU3c1vDoMH8ml",  # Market Map Y Override
    # SEO (Phase E). Last Vendor Scrape drives the visible "last verified" date
    # and JSON-LD datePublished. Meta Title / Description / H1 are only emitted
    # when SEO Reviewed is ticked (the publish gate), so unreviewed drafts never
    # reach the live page. OG Image falls back to Logo in the template.
    "scraped":   "fld1x7M0E6FaSLGVe",  # Last Vendor Scrape
    "seo_title": "fldHXgrEfyTRgbntm",  # Meta Title
    "seo_desc":  "fldVFJD3A5j4kdyfS",  # Meta Description
    "seo_h1":    "fldlrbEQ5nVSlCina",  # SEO H1
    "seo_ok":    "fld9egm24j1kirG80",  # SEO Reviewed (publish gate)
    "og_image":  "fldM0WRHyUqT1h7BX",  # OG Image (multipleAttachments)
    "logo":      "fldrvnMFBLEtwom6T",  # Logo (fallback for OG image)
    "enrichment": "fldQGRQlzeZBeRxGP",  # Enrichment Status (status pill on the page)
}

# Master Variables table field that holds the option name
MASTER_NAME_FIELD = "fld1EI9BDHrdOF92I"

# Vendor URLs registry — the per-vendor source list shown in "Sources and method".
REGISTRY_ID = os.environ.get("AIRTABLE_REGISTRY_TBL", "tblT1Hqk2bEC9xVcR")
RF = {
    "url":          "fldDpTQraitVt4yHB",
    "url_type":     "fldwUVSYu5QYi3N0k",
    "fetched":      "fld7cxhRHwQ6bzFAs",  # Last Fetched
    "hash_changed": "fldjJgczTqipE2Kvn",  # Hash Changed At (drives the Updated badge)
    "active":       "fldFqvuPSinBYvrvk",
    "vendor":       "fldZpeoHlScaEp2yh",  # linked record -> Database record id
}

# ---------------------------------------------------------------------------
# GLOBALS
# ---------------------------------------------------------------------------

LINKED_NAME_CACHE: dict[str, str] = {}
SOURCES_BY_VENDOR: dict[str, list[dict]] = {}


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def slugify(s: str) -> str:
    """Lowercase, ASCII-safe, hyphen-separated slug. Only used for the slug field."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[\(\)]", "", s)
    s = re.sub(r"[/&\\]", "-", s)
    s = re.sub(r"[^a-zA-Z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s).strip("-").lower()
    s = re.sub(r"-+", "-", s)
    return s


def single_select_value(field_val) -> str | None:
    """Extract string value from a singleSelect field (may be dict or str)."""
    if field_val is None:
        return None
    if isinstance(field_val, dict):
        return field_val.get("name")
    return str(field_val)


def linked_names(value) -> list[str]:
    """Resolve a multipleRecordLinks field to a list of display labels."""
    if not value or not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, dict):
            name = item.get("name") or item.get("value", "")
            if name:
                result.append(name)
        elif isinstance(item, str):
            name = LINKED_NAME_CACHE.get(item, "")
            if name:
                result.append(name)
    return result


# ---------------------------------------------------------------------------
# AIRTABLE
# ---------------------------------------------------------------------------

def fetch_records() -> tuple[list[dict], dict[str, str]]:
    """Return (vendor records, id→name map for Master Variables)."""
    api = Api(TOKEN)
    base = api.base(BASE_ID)

    print("  Fetching Master Variables...")
    master = base.table(MASTER_ID)
    master_records = master.all(use_field_ids=True)
    cache: dict[str, str] = {}
    for r in master_records:
        name = r["fields"].get(MASTER_NAME_FIELD, "")
        if name:
            cache[r["id"]] = name
    print(f"  {len(cache)} master variables cached")

    print("  Fetching Database records...")
    db = base.table(TABLE_ID)
    records = db.all(use_field_ids=True)
    print(f"  {len(records)} records fetched")
    return records, cache


def fetch_sources() -> dict[str, list[dict]]:
    """Vendor record id -> sorted source list from the Vendor URLs registry.

    Active URLs only. Each source is {type, url, fetched, updated?}. `updated` is
    set only when the row's Hash Changed At falls on the most recent sweep date
    (the newest Last Fetched across the registry), i.e. the pages the last scan
    actually flagged for review. Content hashes are never emitted.
    """
    api = Api(TOKEN)
    rows = api.base(BASE_ID).table(REGISTRY_ID).all(use_field_ids=True)
    active = [r for r in rows if r["fields"].get(RF["active"])]

    def d(v):  # date portion of a datetime/date value
        return str(v)[:10] if v else ""

    latest_sweep = max((d(r["fields"].get(RF["fetched"])) for r in active), default="")
    by_vendor: dict[str, list[dict]] = {}
    for r in active:
        f = r["fields"]
        url = f.get(RF["url"])
        if not url:
            continue
        src = {
            "type": single_select_value(f.get(RF["url_type"])) or "",
            "url": url,
            "fetched": d(f.get(RF["fetched"])),
        }
        if latest_sweep and d(f.get(RF["hash_changed"])) == latest_sweep:
            src["updated"] = True
        for vid in (f.get(RF["vendor"]) or []):
            by_vendor.setdefault(vid, []).append(src)

    for vid, lst in by_vendor.items():
        lst.sort(key=lambda s: (s["type"], s["url"]))
    print(f"  {len(active)} active source URLs across {len(by_vendor)} vendors (sweep {latest_sweep or 'n/a'})")
    return by_vendor


# ---------------------------------------------------------------------------
# TRANSFORM
# ---------------------------------------------------------------------------

def transform(record: dict, cache: dict[str, str]) -> dict | None:
    """Convert one Airtable record to a vendor object. Returns None if unpublished."""
    global LINKED_NAME_CACHE
    LINKED_NAME_CACHE = cache

    f = record["fields"]

    if not f.get(F["publish"]):
        return None

    name = f.get(F["name"], "")
    if not name:
        return None

    slug = f.get(F.get("slug"), "") or slugify(name)

    # Linked multi-select fields resolve to display labels (no slugify).
    ucf          = linked_names(f.get(F["ucf"], []))
    ai           = linked_names(f.get(F["ai"], []))
    pricing      = linked_names(f.get(F["pricing"], []))
    sdk          = linked_names(f.get(F["sdk"], []))
    integrations = linked_names(f.get(F["integrations"], []))
    warehouse    = linked_names(f.get(F["warehouse"], []))
    compliance   = linked_names(f.get(F["compliance"], []))

    status = (single_select_value(f.get(F["status"])) or "active").lower()

    summary_text = f.get(F["summary"]) or ""

    # MCP type comes from the Airtable "MCP Type" single-select field.
    # Expected option values: Product, Platform, None.
    mcp_server_url = f.get(F["mcp_server_url"])
    mcp_type_raw = (single_select_value(f.get(F["mcp_type"])) or "none").lower()
    mcp_type = mcp_type_raw if mcp_type_raw in ("product", "platform", "none") else "none"

    vendor: dict = {
        "slug":         slug,
        "name":         name,
        "status":       status,
        "h1":           f.get(F["h1"]) or "",
        "h2":           f.get(F["h2"]) or "",
        "ucf":          ucf,
        "mcp":          mcp_type,
        "ai":           ai,
        "pricing":      pricing,
        "sdk":          sdk,
        "integrations": integrations,
        "warehouse":    warehouse,
        "compliance":   compliance,
        "url":          f.get(F["url"]) or "",
        "summary":      summary_text,
    }

    # acquiredBy only on non-active records: prefer dedicated field, else regex.
    if status != "active":
        acquired_by = f.get(F.get("acquired_by")) or None
        if not acquired_by and summary_text:
            m = re.search(r"[Aa]cquired by ([\w][\w &.]+?)(?= in |\.|,)", summary_text)
            if m:
                acquired_by = m.group(1).strip()
        if acquired_by:
            vendor["acquiredBy"] = acquired_by

    # mcpDetail only when an MCP server is present.
    if mcp_type != "none":
        vendor["mcpDetail"] = {
            "url":    mcp_server_url or "",
            "hosted": single_select_value(f.get(F["mcp_hosted"])) or "",
            "docs":   f.get(F["mcp_docs"]) or "",
        }

    # Quadrant map positions. Only emit keys that have a value so the embed can
    # tell "no position yet" (plots at centre + warns) from a real 0.
    def num(field_key):
        v = f.get(F[field_key])
        return round(float(v), 1) if isinstance(v, (int, float)) else None

    for key in ("ax", "ay", "axc", "ayc", "axo", "ayo", "mxo", "myo"):
        val = num(key)
        if val is not None:
            vendor[key] = val

    # SEO fields. Last Vendor Scrape → "last verified"; the Meta/OG fields ride
    # along once created (and only when SEO Reviewed is ticked, the publish gate).
    scraped = f.get(F["scraped"])
    if scraped:
        vendor["scraped"] = str(scraped)[:10]
    if f.get(F["seo_ok"]):  # SEO Reviewed publish gate
        for key, out in (("seo_title", "seoTitle"), ("seo_desc", "seoDesc"), ("seo_h1", "seoH1")):
            val = f.get(F[key])
            if val:
                vendor[out] = val

    # OG image: prefer the dedicated field, fall back to Logo. Attachment fields
    # come back as a list of {url,...}; take the first. (Logo URLs can expire —
    # a padded 1200x630 asset in OG Image is the durable fix.)
    def first_attachment(field_key):
        v = f.get(F[field_key])
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v[0].get("url")
        return None

    og = first_attachment("og_image") or first_attachment("logo")
    if og:
        vendor["ogImage"] = og

    # Sources and method: the live Vendor URLs list + enrichment status pill.
    vendor["sources"] = SOURCES_BY_VENDOR.get(record["id"], [])
    enrichment = single_select_value(f.get(F["enrichment"]))
    if enrichment:
        vendor["enrichment"] = enrichment

    return vendor


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    global SOURCES_BY_VENDOR
    print("Fetching from Airtable...")
    records, cache = fetch_records()
    print("  Fetching Vendor URLs registry...")
    SOURCES_BY_VENDOR = fetch_sources()

    print("Transforming records...")
    vendors: list[dict] = []
    skipped = 0
    for r in records:
        try:
            v = transform(r, cache)
            if v:
                vendors.append(v)
            else:
                skipped += 1
        except Exception as e:
            print(f"  WARN: failed to transform {r.get('id')}: {e}", file=sys.stderr)
            skipped += 1

    print(f"  {len(vendors)} vendors published, {skipped} skipped/unpublished")

    out = {
        "version": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit":  os.environ.get("GITHUB_SHA", "local"),
        "vendors": vendors,
    }

    out_path = OUTPUT_DIR / "testing-tools.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    size_kb = out_path.stat().st_size / 1024
    print(f"Wrote {out_path} ({size_kb:.1f} KB)")
    print("Done.")


if __name__ == "__main__":
    if "--dump-schema" in sys.argv:
        api = Api(TOKEN)
        fields = api.base(BASE_ID).table(TABLE_ID).schema().fields
        for field in fields:
            print(f"  {field.id}  {field.name}")
        sys.exit(0)

    main()
