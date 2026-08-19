#!/usr/bin/env python3
"""
apply.py — P4 of the Proposal Queue. Write the proposals a human APPROVED in the
Airtable `Proposals` table back to the vendor Database table. This is the only
script that writes vendor facts. It stays dormant until P2/P3 have run in shadow
for two scan cycles (brief §10).

For each Proposals row with Status=Approved and no Applied At:
  1. Re-check the field is on the whitelist (never trust the model or the row).
  2. Staleness check: re-read the live Database value; if it no longer equals the
     proposal's Current Value, a human edited it since — set Stale, skip.
  3. Validate Status (lifecycle) values; resolve linked options to record ids.
  4. PATCH the Database record AND set Last Vendor Scrape = today in the same
     write, which flips the Agentic Score Stale formula to RESCORE NEEDED (§11.4).
  5. Status -> Applied (+ Applied At) on success, Failed (+ Error) on failure.
Batch nothing blindly; one record per PATCH keeps failures isolated. Never retry.

Environment:
  AIRTABLE_TOKEN   required — needs data.records:write
  AIRTABLE_BASE_ID default: appRX3rtuXifUnvD4
  APPLY            default: "false" — dry-run: log what WOULD apply, write nothing
"""

from __future__ import annotations
import os
import sys
from datetime import datetime, timezone

try:
    from pyairtable import Api
except ImportError:
    print("ERROR: pip install 'pyairtable>=3,<4'", file=sys.stderr)
    sys.exit(1)

BASE_ID  = os.environ.get("AIRTABLE_BASE_ID", "appRX3rtuXifUnvD4")
DB_TBL   = os.environ.get("AIRTABLE_DATABASE_TBL", "tblOx4tapKq2a0sBR")
MASTER_TBL = os.environ.get("AIRTABLE_MASTER_TBL", "tbl6FWyYcBfcIq64T")
PROP_TBL = os.environ.get("AIRTABLE_PROPOSALS_TBL", "Proposals")
TOKEN    = os.environ.get("AIRTABLE_TOKEN")
APPLY    = os.environ.get("APPLY", "false").lower() == "true"

LAST_SCRAPE = "Last Vendor Scrape"

# Whitelist re-checked here, not just in the prompt (brief §8).
LINKED = {"Pricing Model", "AI / Agentic Capabilities", "MCP Capabilities",
          "Integrations", "Compliance & Security", "SDK Languages", "Use Case Fit"}
SELECT = {"MCP Type": {"Product", "Platform", "None"},
          "Status (lifecycle)": {"Active", "Acquired", "Discontinued", "Sunsetting"}}
NUMERIC = {"Price range (low)", "Price range (high)"}
WHITELIST = LINKED | set(SELECT) | NUMERIC
# Never writable, ever: computed/score fields, overrides, publish gates, voiced.
NEVER = {"Agentic Score Stale", "Publish on Website", "Show in Comparison Matrix",
         "Speero Blurb (MVP 2)"}


def norm_list(v):
    if isinstance(v, list):
        return sorted(x.get("name", x) if isinstance(x, dict) else str(x) for x in v)
    return v


def main() -> None:
    if not TOKEN:
        print("ERROR: AIRTABLE_TOKEN required", file=sys.stderr); sys.exit(1)
    api = Api(TOKEN); base = api.base(BASE_ID)
    props = base.table(PROP_TBL); db = base.table(DB_TBL)

    # Master Variables: name -> record id, for resolving linked-option writes.
    mv = {}
    for r in base.table(MASTER_TBL).all():
        n = r["fields"].get("Name")
        if n:
            mv[n] = r["id"]

    pending = [r for r in props.all() if r["fields"].get("Status") == "Approved"
               and not r["fields"].get("Applied At")]
    print(f"{len(pending)} approved proposals to apply (APPLY={APPLY}).")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    applied = failed = stale = 0

    for r in pending:
        f = r["fields"]
        field = f.get("Target Field")
        vlink = f.get("Vendor") or []
        rid = vlink[0] if vlink and isinstance(vlink[0], str) else (vlink[0].get("id") if vlink else None)
        proposed = f.get("Proposed Value", "")
        current_claim = f.get("Current Value", "")

        def fail(msg):
            nonlocal failed
            failed += 1
            print(f"  FAIL {f.get('Vendor')} {field}: {msg}", file=sys.stderr)
            if APPLY:
                props.update(r["id"], {"Status": "Failed", "Error": msg})

        if field in NEVER or field not in WHITELIST:
            fail(f"field '{field}' not writable"); continue
        if not rid:
            fail("no vendor link"); continue
        if field == "Status (lifecycle)" and proposed not in SELECT[field]:
            fail(f"invalid lifecycle value '{proposed}'"); continue
        if field == "MCP Type" and proposed not in SELECT["MCP Type"]:
            fail(f"invalid MCP Type '{proposed}'"); continue

        live = db.get(rid)["fields"]
        # Staleness: live value must still match what the proposal was based on.
        live_val = norm_list(live.get(field))
        live_str = ", ".join(live_val) if isinstance(live_val, list) else ("" if live_val is None else str(live_val))
        if current_claim and live_str and current_claim.strip() != live_str.strip():
            stale += 1
            print(f"  STALE {f.get('Vendor')} {field}: live='{live_str}' != proposal current='{current_claim}'")
            if APPLY:
                props.update(r["id"], {"Status": "Stale"})
            continue

        # Build the write.
        try:
            if field in LINKED:
                # Additive: append the proposed option(s) as record ids.
                want = [x.strip() for x in str(proposed).replace(";", ",").split(",") if x.strip()]
                ids = [mv[o] for o in want if o in mv]
                if not ids:
                    fail(f"no canonical option matches '{proposed}' (needs a new_canonical_option)"); continue
                existing = [x["id"] if isinstance(x, dict) else x for x in (live.get(field) or [])]
                new_val = list(dict.fromkeys(existing + ids))
                payload = {field: new_val}
            elif field in NUMERIC:
                payload = {field: float(proposed)}
            else:  # single select
                payload = {field: proposed}
            payload[LAST_SCRAPE] = today  # trip the rescore formula (§11.4)
        except Exception as e:
            fail(f"build error: {e}"); continue

        print(f"  APPLY {f.get('Vendor')} {field} <- {proposed}")
        if APPLY:
            try:
                db.update(rid, payload)
                props.update(r["id"], {"Status": "Applied", "Applied At": today})
                applied += 1
            except Exception as e:
                fail(f"PATCH error: {e}")
        else:
            applied += 1  # counts as would-apply in dry run

    verb = "applied" if APPLY else "would apply"
    print(f"Done: {verb} {applied}, failed {failed}, stale {stale}.")


if __name__ == "__main__":
    main()
