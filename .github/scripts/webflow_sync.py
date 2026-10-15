#!/usr/bin/env python3
"""
Reconcile the Webflow "Testing Tools" CMS collection with testing-tools.json,
then purge the jsDelivr cache so the data and any new pages go live promptly.

Runs after sync.py in the GitHub Action. For every vendor in the JSON there
should be exactly one CMS item (name + slug); this script creates the ones that
are missing and deletes the ones whose vendor has left the dataset. The interactive
profile (maps, capabilities, sources) is rendered client-side by island.js, but
the page H1 and the platform overview are written into native CMS fields here
(seo-h1, profile-body) so they exist in the initial HTML for crawlers and no-JS
readers. island.js adopts those nodes rather than rendering its own copies.

Safety:
  - If the JSON has zero vendors (a failed sync), the script makes no deletions.
  - Deletions are capped by MAX_DELETE to avoid a runaway wipe on bad data.
  - Publishing live pages is OFF by default. Keep it off until the Webflow
    template page actually carries the island.js embed, otherwise you would
    publish empty pages. Once the template is live, set WEBFLOW_PUBLISH=true.

Environment variables:
  WEBFLOW_TOKEN         required — Webflow API token with CMS write + publish
  WEBFLOW_SITE_ID       default: 5fbb892601063dd93dd166d7
  WEBFLOW_COLLECTION_ID default: 6a7196c634c1d8c34c1b6cc0
  DATA_FILE             default: testing-tools.json
  WEBFLOW_PUBLISH       default: "false"  ("true" publishes new/changed items live)
  JSDELIVR_PURGE        default: "true"
  MAX_DELETE            default: "10"
"""

from __future__ import annotations
import html
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.webflow.com/v2"

TOKEN = os.environ.get("WEBFLOW_TOKEN")
SITE_ID = os.environ.get("WEBFLOW_SITE_ID", "5fbb892601063dd93dd166d7")
COLLECTION_ID = os.environ.get("WEBFLOW_COLLECTION_ID", "6a7196c634c1d8c34c1b6cc0")
DATA_FILE = os.environ.get("DATA_FILE", "testing-tools.json")
PUBLISH = os.environ.get("WEBFLOW_PUBLISH", "false").lower() == "true"
PURGE = os.environ.get("JSDELIVR_PURGE", "true").lower() == "true"
MAX_DELETE = int(os.environ.get("MAX_DELETE", "10"))

PURGE_URLS = [
    "https://purge.jsdelivr.net/gh/speerotools/testing-tools-data@main/testing-tools.json",
]


def req(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Authorization", "Bearer " + TOKEN)
    r.add_header("accept", "application/json")
    if data is not None:
        r.add_header("Content-Type", "application/json")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if e.code == 429 or e.code >= 500:
                wait = 2 ** attempt
                print(f"  {e.code} on {method} {url}; retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"ERROR {e.code} {method} {url}: {detail}", file=sys.stderr)
            raise
    raise RuntimeError(f"gave up on {method} {url}")


def list_all_items() -> dict[str, str]:
    """Return {slug: item_id} for every item in the collection."""
    out: dict[str, str] = {}
    offset = 0
    while True:
        res = req("GET", f"{API}/collections/{COLLECTION_ID}/items?limit=100&offset={offset}")
        for it in res.get("items", []):
            slug = (it.get("fieldData") or {}).get("slug")
            if slug:
                out[slug] = it["id"]
        total = (res.get("pagination") or {}).get("total", len(out))
        offset += 100
        if offset >= total:
            break
    return out


def create_item(name: str, slug: str) -> None:
    path = "items/live" if PUBLISH else "items"
    body = {"fieldData": {"name": name, "slug": slug}, "isDraft": not PUBLISH, "isArchived": False}
    req("POST", f"{API}/collections/{COLLECTION_ID}/{path}", body)


def delete_item(item_id: str) -> None:
    # Deleting a live item also removes it from the published site.
    try:
        req("DELETE", f"{API}/collections/{COLLECTION_ID}/items/{item_id}/live")
    except urllib.error.HTTPError:
        pass  # item was a draft; nothing published to remove
    req("DELETE", f"{API}/collections/{COLLECTION_ID}/items/{item_id}")


TOOL_BASE = "/ab-testing-tools/"


def seo_h1(v: dict) -> str:
    """The page H1. Same fallback as island.js, so CMS and rendered page agree.

    sync.py only emits seoH1 when SEO Reviewed is ticked, so an unreviewed
    vendor gets the generated fallback, never unreviewed Airtable copy.
    """
    return v.get("seoH1") or f"{v['name']} review: features, pricing, and alternatives"


def profile_body(v: dict) -> str:
    """The factual platform overview (AI Features Summary) as rich text.

    Labelled "Platform overview" because it is the factual summary, not the
    editorial Speero Blurb, which is not published here. Paragraphs follow the
    blank lines in Airtable; everything is escaped.
    """
    paras = [p.strip() for p in (v.get("summary") or "").split("\n\n") if p.strip()]
    if not paras:
        return ""
    body = "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paras)
    return "<h2>Platform overview</h2>" + body


def directory_summary(v: dict) -> str:
    fit = ", ".join(u.split(" (")[0] for u in (v.get("ucf") or [])[:2])
    price = ", ".join(v.get("pricing") or [])
    parts = []
    if fit:
        parts.append(f"Best fit: {fit}.")
    if price:
        parts.append(f"Pricing: {price}.")
    return " ".join(parts)


def native_fields(v: dict) -> dict:
    fd = {
        "seo-h1": seo_h1(v),
        "profile-body": profile_body(v),
        "directory-summary": directory_summary(v),
        "page-url": TOOL_BASE + v["slug"],
    }
    # Head fields stay behind the SEO Reviewed gate, as before.
    if v.get("seoTitle"): fd["meta-title"] = v["seoTitle"]
    if v.get("seoDesc"):  fd["meta-description"] = v["seoDesc"]
    if v.get("ogImage"):  fd["og-image"] = {"url": v["ogImage"]}
    return fd


def publish_site() -> None:
    req("POST", f"{API}/sites/{SITE_ID}/publish", {"publishToWebflowSubdomain": True})


def purge_cdn() -> None:
    for u in PURGE_URLS:
        try:
            with urllib.request.urlopen(u, timeout=30) as resp:
                resp.read()
            print(f"  purged {u}")
        except Exception as e:
            print(f"  WARN purge failed for {u}: {e}", file=sys.stderr)


def main() -> None:
    if not TOKEN:
        print("ERROR: WEBFLOW_TOKEN env var required", file=sys.stderr)
        sys.exit(1)

    with open(DATA_FILE) as f:
        data = json.load(f)
    vendors = data.get("vendors", [])
    desired = {v["slug"]: v["name"] for v in vendors if v.get("slug") and v.get("name")}

    if not desired:
        print("No vendors in JSON — refusing to reconcile (treating as failed sync).", file=sys.stderr)
        sys.exit(1)

    print(f"Reconciling Webflow CMS: {len(desired)} vendors in data.")
    existing = list_all_items()
    print(f"  {len(existing)} items currently in collection.")

    to_create = [(slug, name) for slug, name in desired.items() if slug not in existing]
    to_delete = [(slug, iid) for slug, iid in existing.items() if slug not in desired]

    if len(to_delete) > MAX_DELETE:
        print(f"Refusing to delete {len(to_delete)} items (> MAX_DELETE={MAX_DELETE}). "
              f"Set MAX_DELETE higher if this is intentional.", file=sys.stderr)
        sys.exit(1)

    for slug, name in to_create:
        print(f"  + create {slug} ({name})")
        create_item(name, slug)
    for slug, iid in to_delete:
        print(f"  - delete {slug}")
        delete_item(iid)

    print(f"Created {len(to_create)}, deleted {len(to_delete)}.")

    # Native page content (H1, platform overview, directory line, URL) for
    # every vendor, plus the head-binding SEO fields. sync.py only emits
    # seoTitle/seoDesc/seoH1 when SEO Reviewed is ticked, so unreviewed drafts
    # never reach the CMS; native_fields() falls back exactly as island.js does.
    updated = 0
    items = list_all_items()  # refresh to include newly created items
    for v in vendors:
        iid = items.get(v.get("slug"))
        if not iid:
            continue
        path = f"items/{iid}/live" if PUBLISH else f"items/{iid}"
        req("PATCH", f"{API}/collections/{COLLECTION_ID}/{path}", {"fieldData": native_fields(v)})
        updated += 1
    print(f"Native and SEO fields updated on {updated} items.")

    if PUBLISH and (to_create or to_delete or updated):
        print("Publishing site so page changes go live...")
        publish_site()

    if PURGE:
        print("Purging jsDelivr cache...")
        purge_cdn()

    print("Done.")


if __name__ == "__main__":
    main()
