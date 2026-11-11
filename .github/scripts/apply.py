#!/usr/bin/env python3
"""
apply.py: P4 of the Proposal Queue. Write the proposals a human APPROVED in the
Airtable `Proposals` table back to the vendor Database table. This is the only
script that writes vendor facts. apply.yml runs it daily with APPLY=true and on
manual dispatch (dry run by default).

For each Proposals row with Status=Approved and no Applied At:
  1. Re-check the field is on the whitelist (never trust the model or the row).
  2. Linked fields are additive: resolve the proposed option(s) to Master
     Variables record ids WITHIN that field's category (case-insensitive, whole
     value first) and append. No staleness check here: appending cannot clobber a
     human edit, and a sibling proposal on the same field must not cancel this one.
  3. Single-select and numeric fields overwrite, so they keep the staleness check:
     if the live value no longer matches the proposal's Current Value, a human
     edited it since; set Stale and skip.
  4. PATCH the Database record AND set Last Vendor Scrape = today in the same
     write, which flips the Agentic Score Stale formula to RESCORE NEEDED (§11.4).
  5. Status -> Applied (+ Applied At) on success, Failed (+ Error) on failure.
One record per PATCH keeps failures isolated, and one bad row never stops the
rest. Outcomes go to Slack, and unexpected errors make the run exit non-zero so a
broken daily auto-apply shows up in Actions.

Environment:
  AIRTABLE_TOKEN     required, needs data.records:write
  AIRTABLE_BASE_ID   default: appRX3rtuXifUnvD4
  APPLY              default: "false" (dry run: log what WOULD apply, write nothing)
  SLACK_WEBHOOK_URL  optional; a short summary is posted when anything was applied
  SLACK_MENTION      optional reviewer mention (default Ben, <@U0GP0BBEW>)
"""

from __future__ import annotations
import json
import os
import sys
import urllib.request
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
WEBHOOK  = os.environ.get("SLACK_WEBHOOK_URL")
MENTION  = os.environ.get("SLACK_MENTION") or "<@U0GP0BBEW>"
QUEUE_URL = "https://airtable.com/appRX3rtuXifUnvD4/tblsqMlhnvDrVbifc/viwgR2TrZowcuEcKc?blocks=hide"

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


def option_index(base) -> dict:
    """(category, option), both lowercase -> Master Variables record id. The
    category is the "Variable" field (as in the canonical sync/extract.py); keying
    by it stops a same-named option of another category being linked."""
    idx = {}
    for r in base.table(MASTER_TBL).all():
        f = r["fields"]
        name = str(f.get("Name") or "").strip()
        cat = f.get("Variable") or f.get("Category") or f.get("Type") or ""
        cat = str(cat.get("name", "") if isinstance(cat, dict) else cat).strip().lower()
        if name and cat:
            idx[(cat, name.lower())] = r["id"]
    return idx


def resolve(field: str, proposed, idx: dict) -> tuple[list | None, list]:
    """Proposed value -> (record ids, []) or (None, missing parts). The whole value
    is tried first, so an option whose name contains a comma still matches."""
    cat = field.lower()
    whole = idx.get((cat, str(proposed).strip().lower()))
    if whole:
        return [whole], []
    parts = [x.strip() for x in str(proposed).replace(";", ",").split(",") if x.strip()]
    ids = [idx.get((cat, x.lower())) for x in parts]
    missing = [x for x, i in zip(parts, ids) if not i]
    if missing or not parts:
        return None, (missing or [str(proposed)])
    return ids, []


def same(a, b) -> bool:
    """Loose equality for the staleness check: numbers compare numerically
    ('1' equals 1.0), text ignores case and surrounding space."""
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a or "").strip().lower() == str(b or "").strip().lower()


def set_status(props, row_id: str, fields: dict) -> None:
    """Proposals status write with one retry. It runs AFTER the Database write, so
    a hiccup here must not turn an applied change into Failed (if it still fails,
    the next run sees the value already live and marks the row Applied)."""
    err = None
    for _ in range(2):
        try:
            props.update(row_id, fields)
            return
        except Exception as e:
            err = e
    print(f"  WARN could not set {fields} on {row_id}: {err}", file=sys.stderr)


def esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def post_slack(lines: list[str]) -> None:
    if not (WEBHOOK and lines):
        return
    req = urllib.request.Request(WEBHOOK, data=json.dumps({"text": "\n".join(lines)}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except Exception as e:
        print(f"WARN Slack post failed: {e}", file=sys.stderr)


def main() -> None:
    if not TOKEN:
        print("ERROR: AIRTABLE_TOKEN required", file=sys.stderr)
        sys.exit(1)
    base = Api(TOKEN).base(BASE_ID)
    props = base.table(PROP_TBL)
    db = base.table(DB_TBL)
    idx = option_index(base)

    pending = [r for r in props.all() if r["fields"].get("Status") == "Approved"
               and not r["fields"].get("Applied At")]
    print(f"{len(pending)} approved proposals to apply (APPLY={APPLY}).")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    done = {"applied": [], "present": [], "failed": [], "stale": []}
    errors = 0

    for r in pending:
        f = r["fields"]
        field = f.get("Target Field")
        vlink = f.get("Vendor") or []
        rid = vlink[0] if vlink and isinstance(vlink[0], str) else (vlink[0].get("id") if vlink else None)
        proposed = f.get("Proposed Value", "")
        current_claim = f.get("Current Value", "")
        label = f"{field}: {proposed}"

        def fail(msg):
            done["failed"].append(f"{label} ({msg})")
            print(f"  FAIL {label}: {msg}", file=sys.stderr)
            if APPLY:
                set_status(props, r["id"], {"Status": "Failed", "Error": msg[:1000]})

        if field in NEVER or field not in WHITELIST:
            fail(f"field '{field}' is not writable"); continue
        if not rid:
            fail("no vendor link"); continue
        if field in SELECT and proposed not in SELECT[field]:
            fail(f"'{proposed}' is not a valid {field}"); continue
        try:
            live = db.get(rid)["fields"]
        except Exception as e:
            errors += 1
            fail(f"could not read the vendor record: {e}"); continue
        label = f"{live.get('Name', rid)} · {field}: {proposed}"

        if field in LINKED:
            ids, missing = resolve(field, proposed, idx)
            if ids is None:
                fail(f"'{', '.join(missing)}' is not a {field} option in Master Variables; "
                     f"add it there, then set this row back to Approved"); continue
            existing = [x["id"] if isinstance(x, dict) else x for x in (live.get(field) or [])]
            add = [i for i in ids if i not in existing]
            if not add:
                done["present"].append(label)
                if APPLY:
                    set_status(props, r["id"], {"Status": "Applied", "Applied At": today})
                continue
            payload = {field: existing + add}
        else:
            live_val = live.get(field)
            if live_val not in (None, "") and same(live_val, proposed):
                done["present"].append(label)
                if APPLY:
                    set_status(props, r["id"], {"Status": "Applied", "Applied At": today})
                continue
            if current_claim and live_val not in (None, "") and not same(current_claim, live_val):
                done["stale"].append(f"{label} (live value is now '{live_val}')")
                print(f"  STALE {label}: live='{live_val}' != proposal current='{current_claim}'")
                if APPLY:
                    set_status(props, r["id"], {"Status": "Stale"})
                continue
            if field in NUMERIC:
                try:
                    v = float(proposed)
                except (TypeError, ValueError):
                    fail(f"'{proposed}' is not a number"); continue
                payload = {field: int(v) if v.is_integer() else v}
            else:
                payload = {field: proposed}
        payload[LAST_SCRAPE] = today  # trip the rescore formula (§11.4)

        print(f"  APPLY {label}")
        if not APPLY:
            done["applied"].append(label)
            continue
        try:
            db.update(rid, payload)
        except Exception as e:
            errors += 1
            fail(f"Database write failed: {e}"); continue
        done["applied"].append(label)
        set_status(props, r["id"], {"Status": "Applied", "Applied At": today})

    verb = "applied" if APPLY else "would apply"
    print(f"Done: {verb} {len(done['applied'])}, already present {len(done['present'])}, "
          f"failed {len(done['failed'])}, stale {len(done['stale'])}.")

    if APPLY and any(done.values()):
        needs_eyes = bool(done["failed"] or done["stale"])
        lines = [(MENTION + " " if needs_eyes else "") +
                 f"*Auto-apply {today}:* {len(done['applied'])} applied, {len(done['present'])} already "
                 f"present, {len(done['failed'])} failed, {len(done['stale'])} stale."]
        for title, items in (("Failed", done["failed"]),
                             ("Stale (the live value changed after the proposal was made)", done["stale"])):
            if items:
                lines.append(f"*{title}*")
                lines += [f"• {esc(x)}" for x in items[:20]]
                if len(items) > 20:
                    lines.append(f"  …and {len(items) - 20} more")
        lines.append(f"Review: {QUEUE_URL}")
        post_slack(lines)

    if errors:
        print(f"ERROR: {errors} unexpected Airtable error(s); see the FAIL lines above.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
