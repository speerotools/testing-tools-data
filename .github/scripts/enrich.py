#!/usr/bin/env python3
"""
enrich.py — content-hash monitor for the vendor URL registry.

Fetches every Active URL with the standard library (no Firecrawl, no scraping
dependency), reduces the HTML to stable visible text, hashes it, and compares
against the stored hash to detect changed pages.

Two modes, one script:

  --baseline   Fetch every Active URL, hash it, write the hash and the scrape
               date back to Airtable. Run once to establish the baseline.

  (default)    Monitor run. Fetch again, compare each hash to the stored one,
               write the new hash + date back, and emit a diff of the URLs
               whose content changed, grouped by vendor. That diff routes into
               targeted re-verification.

Only the registry's hash and scrape-date fields are ever written. Rows are
never deleted or blanked. Dead URLs are flagged for a human, not removed.

Why normalize the HTML: a raw-bytes hash flips on every rotating CSRF token,
analytics nonce, or timestamp embedded in markup, producing false "changed"
signals. We strip <script>/<style>/comments and collapse whitespace so the
hash tracks visible content, not per-request noise.

Hard rules from the dev handoff:
  - Airtable PATCH batches are capped at 25 (the API silently no-ops at 50).
  - Never trust the HTTP success response; the run prints a written-count so
    you can spot-check the table afterwards.
  - uniform.dev/trust is robots-blocked; skipped by design, not retried.
  - A failed fetch leaves the row untouched.

Environment variables:
  AIRTABLE_TOKEN        required — PAT with data.records:read + data.records:write
  AIRTABLE_BASE_ID      default: appRX3rtuXifUnvD4
  AIRTABLE_REGISTRY_TBL default: tblT1Hqk2bEC9xVcR
  ENRICH_LIMIT          optional — cap URLs processed (for a quick test run)
  FETCH_DELAY           default: 0.5 — seconds to pause between fetches (be polite)
  DIFF_OUT              default: changed-pages.md
"""

from __future__ import annotations
import hashlib
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from html.parser import HTMLParser

try:
    from pyairtable import Api
except ImportError:
    print("ERROR: pyairtable not installed. Run: pip install 'pyairtable>=3,<4'", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

BASE_ID      = os.environ.get("AIRTABLE_BASE_ID", "appRX3rtuXifUnvD4")
REGISTRY_TBL = os.environ.get("AIRTABLE_REGISTRY_TBL", "tblT1Hqk2bEC9xVcR")
DIFF_OUT     = os.environ.get("DIFF_OUT", "changed-pages.md")
LIMIT        = int(os.environ.get("ENRICH_LIMIT", "0")) or None
FETCH_DELAY  = float(os.environ.get("FETCH_DELAY", "0.5"))

AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")

USER_AGENT = "SpeeroToolMonitor/1.0 (+https://speero.com; content-change check)"

# Registry field IDs (from the dev handoff doc).
F = {
    "url":       "fldDpTQraitVt4yHB",
    "vendor":    "fldZpeoHlScaEp2yh",
    "active":    "fldFqvuPSinBYvrvk",
    "source":    "fldIaxgENIkgmOA63",
    "url_type":  "fldwUVSYu5QYi3N0k",
    "notes":     "fldLvNFcAsuacgV3W",
    # TODO: fill these two from `python enrich.py --dump-schema`.
    "hash":         "fldCONTENTHASHXXX",
    "last_scrape":  "fldLASTSCRAPEXXX",
}

BATCH = 25  # Airtable hard limit; do NOT raise (50 silently writes nothing).

SKIP_URLS = {"uniform.dev/trust"}


# ---------------------------------------------------------------------------
# FETCH + NORMALIZE (stdlib only)
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    """Collect visible text, dropping <script>/<style> contents."""
    _DROP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._DROP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self._DROP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip == 0:
            t = data.strip()
            if t:
                self.parts.append(t)


def normalize_html(html: str) -> str:
    """Reduce HTML to collapsed visible text for a stable hash."""
    try:
        p = _TextExtractor()
        p.feed(html)
        text = " ".join(p.parts)
    except Exception:
        # Fall back to a crude tag strip if the parser chokes.
        text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def fetch_url(url: str) -> str | None:
    """Fetch a URL with the standard library. Returns normalized text or None."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                ctype = resp.headers.get("Content-Type", "")
                if "html" not in ctype and "xml" not in ctype and ctype:
                    # Non-HTML (PDF, image, JSON): hash raw bytes instead.
                    return hashlib.sha256(resp.read()).hexdigest()
                charset = resp.headers.get_content_charset() or "utf-8"
                raw = resp.read().decode(charset, "ignore")
            return normalize_html(raw)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            print(f"  fetch {e.code} for {url}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"  fetch error for {url}: {e}", file=sys.stderr)
            return None
    return None


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


# ---------------------------------------------------------------------------
# AIRTABLE
# ---------------------------------------------------------------------------

def registry_table():
    return Api(AIRTABLE_TOKEN).base(BASE_ID).table(REGISTRY_TBL)


def fetch_active_rows(table) -> list[dict]:
    rows = table.all(use_field_ids=True)
    active = [r for r in rows if r["fields"].get(F["active"])]
    if LIMIT:
        active = active[:LIMIT]
    return active


def write_batches(table, updates: list[dict]) -> int:
    written = 0
    for i in range(0, len(updates), BATCH):
        chunk = updates[i:i + BATCH]
        table.batch_update(chunk, use_field_ids=True)
        written += len(chunk)
    return written


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def vendor_label(fields: dict) -> str:
    v = fields.get(F["vendor"])
    if isinstance(v, list) and v:
        return v[0] if isinstance(v[0], str) else (v[0].get("name") or "Unknown")
    return "Unknown"


def main(baseline: bool) -> None:
    if not AIRTABLE_TOKEN:
        print("ERROR: AIRTABLE_TOKEN is required", file=sys.stderr)
        sys.exit(1)
    if "XXX" in F["hash"] or "XXX" in F["last_scrape"]:
        print("ERROR: hash / last_scrape field IDs are still placeholders. "
              "Run `python enrich.py --dump-schema` and set them in F.", file=sys.stderr)
        sys.exit(1)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    table = registry_table()
    rows = fetch_active_rows(table)
    print(f"{len(rows)} active URLs to process ({'baseline' if baseline else 'monitor'} mode).")

    updates: list[dict] = []
    changed: dict[str, list[str]] = defaultdict(list)
    failures: list[str] = []
    skipped = 0

    for r in rows:
        f = r["fields"]
        url = f.get(F["url"])
        if not url:
            continue
        if any(s in url for s in SKIP_URLS):
            skipped += 1
            continue

        content = fetch_url(url)
        if FETCH_DELAY:
            time.sleep(FETCH_DELAY)
        if content is None:
            failures.append(url)
            continue

        new_hash = content_hash(content)
        old_hash = f.get(F["hash"])
        if not baseline and old_hash and old_hash != new_hash:
            changed[vendor_label(f)].append(url)

        updates.append({"id": r["id"], "fields": {F["hash"]: new_hash, F["last_scrape"]: today}})

    written = write_batches(table, updates)
    print(f"Wrote {written} rows. Skipped {skipped}. Failures: {len(failures)}.")
    if failures:
        print("Failed URLs (left untouched):", file=sys.stderr)
        for u in failures:
            print("  " + u, file=sys.stderr)

    print("\nSpot-check reminder: open the registry table and confirm hash + "
          "scrape date are visibly populated on a handful of rows before trusting this run.")

    if not baseline:
        total_changed = sum(len(v) for v in changed.values())
        lines = [f"# Changed pages — {today}", "",
                 f"{total_changed} URL(s) across {len(changed)} vendor(s) changed since last run.", ""]
        for vendor in sorted(changed):
            lines.append(f"## {vendor}")
            for u in changed[vendor]:
                lines.append(f"- {u}")
            lines.append("")
        with open(DIFF_OUT, "w") as fh:
            fh.write("\n".join(lines))
        print(f"\nDiff written to {DIFF_OUT}: {total_changed} changed URL(s).")

    print("Done.")


if __name__ == "__main__":
    if "--dump-schema" in sys.argv:
        if not AIRTABLE_TOKEN:
            print("ERROR: AIRTABLE_TOKEN required", file=sys.stderr)
            sys.exit(1)
        fields = Api(AIRTABLE_TOKEN).base(BASE_ID).table(REGISTRY_TBL).schema().fields
        for field in fields:
            print(f"  {field.id}  {field.name}")
        sys.exit(0)
    main(baseline="--baseline" in sys.argv)
