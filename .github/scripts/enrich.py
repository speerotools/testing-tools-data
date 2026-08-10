#!/usr/bin/env python3
"""
enrich.py — Job 1 of the monthly vendor freshness loop: fetch + hash + triage.

Fetches every Active URL in the Vendor URLs registry with the standard library
(no Firecrawl), reduces the HTML to stable visible text, hashes it, and diffs
against the stored hash. Changed URLs are triaged by severity and reported;
nothing about the vendor Database table is written or proposed here. Approval
and any Database writes happen out of band (the "apply" half of the loop).

Modes:
  --baseline   Establish the hash baseline: write Last Content Hash, Last
               Fetched, Last Status Code for every Active URL. No diff/triage.
  (default)    Monitor run. Fetch again, apply the hash-diff contract, triage
               what changed, and write changed-pages.md + scan-summary.json.

Hash-diff contract (per docs/handoffs/monthly-freshness-loop.md §3):
  on change  -> Last Content Hash -> Previous Content Hash, stamp Hash Changed
               At = today, bump Change Streak.
  no change  -> Change Streak reset to 0.
  every run  -> Last Fetched + Last Status Code written; hash left untouched on
               a failed fetch so a 404 never blanks a good hash.

Invariants (brief §8):
  - Airtable PATCH limit is 10 records per request.
  - This script NEVER stamps Last Vendor Scrape (that is the apply step's job)
    and never touches the Database table or any voiced field.
  - A failed fetch leaves the hash untouched; rows are never deleted.
  - uniform.dev/trust is robots-blocked; skipped by design.

Environment variables:
  AIRTABLE_TOKEN        required — PAT with data.records:read + data.records:write
  AIRTABLE_BASE_ID      default: appRX3rtuXifUnvD4
  AIRTABLE_REGISTRY_TBL default: tblT1Hqk2bEC9xVcR
  AIRTABLE_DATABASE_TBL default: tblOx4tapKq2a0sBR  (vendor-name lookup only)
  ENRICH_LIMIT          optional — cap URLs processed (quick test run)
  FETCH_DELAY           default: 0.5 — seconds between fetches
  DIFF_OUT              default: changed-pages.md
  SUMMARY_OUT           default: scan-summary.json
"""

from __future__ import annotations
import hashlib
import json
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
DATABASE_TBL = os.environ.get("AIRTABLE_DATABASE_TBL", "tblOx4tapKq2a0sBR")
DIFF_OUT     = os.environ.get("DIFF_OUT", "changed-pages.md")
SUMMARY_OUT  = os.environ.get("SUMMARY_OUT", "scan-summary.json")
_lim         = os.environ.get("ENRICH_LIMIT", "").strip()
LIMIT        = int(_lim) if _lim.isdigit() and int(_lim) > 0 else None
FETCH_DELAY  = float(os.environ.get("FETCH_DELAY", "0.5"))

AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
USER_AGENT = "SpeeroToolMonitor/1.0 (+https://speero.com; content-change check)"

# Registry read/written by FIELD NAME (use_field_ids=False) so that URL Type
# resolves to its option label, which the triage below keys on.
FN = {
    "url": "URL", "vendor": "Vendor", "active": "Active", "url_type": "URL Type",
    "notes": "Notes", "hash": "Last Content Hash", "fetched": "Last Fetched",
    "status": "Last Status Code", "prev_hash": "Previous Content Hash",
    "changed_at": "Hash Changed At", "streak": "Change Streak",
}

BATCH = 10  # Airtable PATCH hard limit; the API silently no-ops above this.
SKIP_URLS = {"uniform.dev/trust"}
NOISY_STREAK = 3


# ---------------------------------------------------------------------------
# FETCH + NORMALIZE (stdlib only)
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
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
    try:
        p = _TextExtractor()
        p.feed(html)
        text = " ".join(p.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
    # strip rotating noise: ISO timestamps, long hex nonces, cache-buster digits
    text = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}[:\d]*", " ", text)
    text = re.sub(r"\b[0-9a-f]{16,}\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_url(url: str):
    """Return (normalized_text_or_None, status_code_or_None)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                code = getattr(resp, "status", 200) or 200
                ctype = resp.headers.get("Content-Type", "")
                if "html" not in ctype and "xml" not in ctype and ctype:
                    return hashlib.sha256(resp.read()).hexdigest(), code
                charset = resp.headers.get_content_charset() or "utf-8"
                raw = resp.read().decode(charset, "ignore")
            return normalize_html(raw), code
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt); continue
            return None, e.code
        except Exception as e:
            print(f"  fetch error for {url}: {e}", file=sys.stderr)
            return None, None
    return None, None


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


# ---------------------------------------------------------------------------
# AIRTABLE
# ---------------------------------------------------------------------------

def api():
    return Api(AIRTABLE_TOKEN)


def vendor_names() -> dict[str, str]:
    """record id -> vendor Name, for readable grouping."""
    try:
        rows = api().base(BASE_ID).table(DATABASE_TBL).all(fields=["Name"])
        return {r["id"]: r["fields"].get("Name", r["id"]) for r in rows}
    except Exception as e:
        print(f"  WARN vendor-name lookup failed: {e}", file=sys.stderr)
        return {}


def active_rows(table) -> list[dict]:
    rows = table.all()  # by name
    active = [r for r in rows if r["fields"].get(FN["active"])]
    return active[:LIMIT] if LIMIT else active


def write_batches(table, updates: list[dict]) -> int:
    written = 0
    for i in range(0, len(updates), BATCH):
        table.batch_update(updates[i:i + BATCH])
        written += len(updates[i:i + BATCH])
    return written


# ---------------------------------------------------------------------------
# TRIAGE (brief §4)
# ---------------------------------------------------------------------------

def severity(url_type: str, status, streak: int) -> str:
    if status is not None and (status >= 400 or 300 <= status < 400):
        return "error"
    if streak and streak >= NOISY_STREAK:
        return "noisy"
    t = (url_type or "").lower()
    if "pricing" in t:
        return "high"
    if any(k in t for k in ("marketing", "home", "landing")):
        return "low"
    return "normal"  # docs / changelog / feature / api / other


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def vlabel(fields: dict, names: dict) -> str:
    v = fields.get(FN["vendor"])
    if isinstance(v, list) and v:
        rid = v[0] if isinstance(v[0], str) else v[0].get("id", "")
        return names.get(rid, rid or "Unknown")
    return "Unknown"


def main(baseline: bool) -> None:
    if not AIRTABLE_TOKEN:
        print("ERROR: AIRTABLE_TOKEN is required", file=sys.stderr)
        sys.exit(1)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    table = api().base(BASE_ID).table(REGISTRY_TBL)
    names = {} if baseline else vendor_names()
    rows = active_rows(table)
    print(f"{len(rows)} active URLs ({'baseline' if baseline else 'monitor'} mode).")

    updates: list[dict] = []
    buckets: dict[str, list[dict]] = defaultdict(list)  # severity -> [{vendor,url,type,status,streak}]
    failures = 0
    skipped = 0

    for r in rows:
        f = r["fields"]
        url = f.get(FN["url"])
        if not url:
            continue
        if any(s in url for s in SKIP_URLS):
            skipped += 1
            continue

        payload, status = fetch_url(url)
        if FETCH_DELAY:
            time.sleep(FETCH_DELAY)

        fields = {FN["fetched"]: today}
        if status is not None:
            fields[FN["status"]] = status

        if payload is None:
            failures += 1
            # Record the fetch + status so triage sees the error; keep the hash.
            updates.append({"id": r["id"], "fields": fields})
            if not baseline:
                buckets["error"].append({"vendor": vlabel(f, names), "url": url,
                                         "type": f.get(FN["url_type"]) or "", "status": status,
                                         "streak": f.get(FN["streak"]) or 0})
            continue

        new_hash = content_hash(payload)
        old_hash = f.get(FN["hash"])
        old_streak = f.get(FN["streak"]) or 0
        fields[FN["hash"]] = new_hash

        if baseline:
            updates.append({"id": r["id"], "fields": fields})
            continue

        if old_hash and old_hash != new_hash:
            fields[FN["prev_hash"]] = old_hash
            fields[FN["changed_at"]] = today
            fields[FN["streak"]] = int(old_streak) + 1
            sev = severity(f.get(FN["url_type"]), status, int(old_streak) + 1)
            buckets[sev].append({"vendor": vlabel(f, names), "url": url,
                                 "type": f.get(FN["url_type"]) or "", "status": status,
                                 "streak": int(old_streak) + 1})
        elif old_hash:
            if old_streak:
                fields[FN["streak"]] = 0
        updates.append({"id": r["id"], "fields": fields})

    written = write_batches(table, updates)
    print(f"Wrote {written} rows. Skipped {skipped}. Fetch failures: {failures}.")
    print("Spot-check: confirm hash + Last Fetched populated on a few rows before trusting this run.")

    if baseline:
        print("Baseline done.")
        return

    # ---- report (brief §5) ----
    order = ["high", "normal", "low", "error", "noisy"]
    label = {"high": "HIGH — pricing changed", "normal": "CHANGED — needs review",
             "low": "LOW — marketing copy churn", "error": "ERRORS", "noisy": "NOISY (likely hash noise)"}
    changed_vendors = {b["vendor"] for s in ("high", "normal", "low") for b in buckets[s]}
    lines = [f"# Vendor scan — {today}", "",
             f"Scanned {len(rows)} URLs. {len(changed_vendors)} vendor(s) with real changes, "
             f"{len(buckets['error'])} error(s), {len(buckets['noisy'])} noisy.", ""]
    for sev in order:
        if not buckets[sev]:
            continue
        lines.append(f"## {label[sev]} ({len(buckets[sev])})")
        by_v = defaultdict(list)
        for b in buckets[sev]:
            by_v[b["vendor"]].append(b)
        for vendor in sorted(by_v):
            for b in by_v[vendor]:
                extra = f" [{b['type']}]" if b["type"] else ""
                st = f" status {b['status']}" if b["status"] and b["status"] >= 300 else ""
                streak = f" streak {b['streak']}" if b["streak"] >= NOISY_STREAK else ""
                lines.append(f"- {vendor} — {b['url']}{extra}{st}{streak}")
        lines.append("")
    with open(DIFF_OUT, "w") as fh:
        fh.write("\n".join(lines))

    summary = {
        "date": today, "scanned": len(rows),
        "counts": {s: len(buckets[s]) for s in order},
        "changed_vendors": sorted(changed_vendors),
        "buckets": {s: buckets[s] for s in order},
    }
    with open(SUMMARY_OUT, "w") as fh:
        json.dump(summary, fh, indent=2)
    print(f"Wrote {DIFF_OUT} and {SUMMARY_OUT}. "
          f"changed={len(changed_vendors)} errors={len(buckets['error'])} noisy={len(buckets['noisy'])}")
    print("Done.")


if __name__ == "__main__":
    if "--dump-schema" in sys.argv:
        if not AIRTABLE_TOKEN:
            print("ERROR: AIRTABLE_TOKEN required", file=sys.stderr)
            sys.exit(1)
        for field in api().base(BASE_ID).table(REGISTRY_TBL).schema().fields:
            print(f"  {field.id}  {field.name}")
        sys.exit(0)
    main(baseline="--baseline" in sys.argv)
