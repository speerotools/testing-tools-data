#!/usr/bin/env python3
"""
create_proposals_table.py — one-time setup. Creates the fully-typed `Proposals`
table (brief §5) in the Testing Tools base via Airtable's metadata API, so you
don't have to build 17 fields and their select options by hand.

Run once locally:
    AIRTABLE_TOKEN=pat... python3 create_proposals_table.py

The token needs the `schema.bases:write` scope (in addition to the read/write
data scopes it already has). If it doesn't, add that scope in the Airtable
developer hub and re-run.

Note: Airtable requires the primary (first) field to be a text-like type, so the
primary field is a single-line `Proposal` label; `Proposal ID` (auto number)
follows it, exactly as the brief lists otherwise. The Pending Review *view*
(filter Status = Pending) is a manual one-click step afterwards — the API can
create views but can't set their filters reliably.
"""

from __future__ import annotations
import json
import os
import sys
import urllib.request

BASE_ID  = os.environ.get("AIRTABLE_BASE_ID", "appRX3rtuXifUnvD4")
DB_TBL   = os.environ.get("AIRTABLE_DATABASE_TBL", "tblOx4tapKq2a0sBR")
TOKEN    = os.environ.get("AIRTABLE_TOKEN")

WHITELIST = ["Pricing Model", "Price range (low)", "Price range (high)",
             "AI / Agentic Capabilities", "MCP Capabilities", "MCP Type",
             "Integrations", "Compliance & Security", "SDK Languages",
             "Use Case Fit", "Status (lifecycle)"]

DATE = {"type": "date", "options": {"dateFormat": {"name": "iso"}}}


def choices(names):
    return {"choices": [{"name": n} for n in names]}


FIELDS = [
    # Primary must be text-like; also serves as the identifier. (autoNumber and
    # other computed types can't be created via the metadata API — add an
    # autonumber column in the UI afterwards if you want one.)
    {"name": "Proposal", "type": "singleLineText"},
    {"name": "Vendor", "type": "multipleRecordLinks", "options": {"linkedTableId": DB_TBL}},
    {"name": "Target Field", "type": "singleSelect", "options": choices(WHITELIST)},
    {"name": "Current Value", "type": "multilineText"},
    {"name": "Proposed Value", "type": "multilineText"},
    {"name": "Evidence Quote", "type": "multilineText"},
    {"name": "Source URL", "type": "url"},
    {"name": "Confidence", "type": "number", "options": {"precision": 2}},
    {"name": "Rationale", "type": "multilineText"},
    {"name": "Change Type", "type": "singleSelect", "options": choices(["vendor_change", "baseline_correction"])},
    {"name": "Scan Date", **DATE},
    {"name": "Diff Link", "type": "url"},
    {"name": "Status", "type": "singleSelect",
     "options": choices(["Pending", "Approved", "Rejected", "Applied", "Failed", "Stale"])},
    {"name": "Reviewed By", "type": "singleCollaborator"},
    {"name": "Reviewed At", **DATE},
    {"name": "Applied At", **DATE},
    {"name": "Error", "type": "multilineText"},
]


def main() -> None:
    if not TOKEN:
        print("ERROR: AIRTABLE_TOKEN required (needs schema.bases:write)", file=sys.stderr)
        sys.exit(1)
    body = {"name": "Proposals",
            "description": "Monthly-scan change proposals for human review (see docs).",
            "fields": FIELDS}
    url = f"https://api.airtable.com/v0/meta/bases/{BASE_ID}/tables"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + TOKEN,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            res = json.loads(resp.read().decode())
        print(f"Created table 'Proposals' (id {res.get('id')}) with {len(res.get('fields', []))} fields.")
        print("Next: add a grid view named 'Pending Review', filter Status = Pending,")
        print("sort Confidence desc, and reorder Target Field / Proposed Value / Evidence Quote to the left.")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        print(f"ERROR {e.code}: {detail}", file=sys.stderr)
        if "INVALID_PERMISSIONS" in detail or e.code in (401, 403):
            print("The token is missing schema.bases:write — add it in the Airtable developer hub.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
