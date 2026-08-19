#!/usr/bin/env python3
"""
assess.py — P2 of the Proposal Queue. Turn snapshot diffs into reviewable
Airtable change proposals. Writes NOTHING to the vendor Database table; it only
creates Pending rows in the `Proposals` table for a human to approve (apply.py
does the writing).

Runs after enrich.py in the same workflow, BEFORE the snapshots are committed, so
`git diff HEAD -- snapshots/<path>` is exactly this run's change set. Only URLs
whose hash moved (the high/normal/low buckets in scan-summary.json) are assessed.

Pipeline: changed URL -> unified snapshot diff + current record + allowed options
-> Gemini (native structured output) -> validated proposals -> Proposals rows.

Guardrails (brief §4, §8, §11):
  - Field whitelist (WHITELIST). Anything else is dropped here, and re-checked
    again in apply.py — a schema constrains shape, not truthfulness.
  - Every proposal needs a verbatim evidence quote + source URL, else dropped.
  - Cap 50 proposals per run; abort loudly past that.
  - Status (lifecycle): only Active/Acquired/Discontinued/Sunsetting, and a
    404/dead URL never justifies Discontinued — enforced in the prompt and in
    apply.py.
  - Linked fields must resolve to existing Master Variables options, else the
    model emits proposal_type=new_canonical_option for human review.

Environment:
  GEMINI_API_KEY   required
  GEMINI_MODEL     default: gemini-2.5-flash
  AIRTABLE_TOKEN   required (read Database/Master/registry, write Proposals)
  AIRTABLE_BASE_ID default: appRX3rtuXifUnvD4
  SUMMARY_IN       default: scan-summary.json
  SHADOW           default: "true" — call Gemini, write proposals.json, but do
                   NOT create Airtable rows
  DRY_RUN          default: "false" — list changed URLs + diff sizes and exit
                   WITHOUT calling Gemini (free; for pipeline testing)
  MAX_ASSESS       default: 40 — abort before any Gemini call if more URLs than
                   this changed (a flood means noise, not real change — protects
                   the API bill)
  ASSESS_LOW       default: "false" — skip the low/marketing-homepage bucket
                   (churniest, near-zero factual yield) to save Gemini calls
  MAX_PROPOSALS    default: 50

Cost note: one Gemini call per changed URL. Expected volume ~7-35/month. The
MAX_ASSESS guard and skipping 'low' keep a bad scan from running up the bill.
"""

from __future__ import annotations
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request

try:
    from pyairtable import Api
except ImportError:
    print("ERROR: pip install 'pyairtable>=3,<4'", file=sys.stderr)
    sys.exit(1)

BASE_ID   = os.environ.get("AIRTABLE_BASE_ID", "appRX3rtuXifUnvD4")
DB_TBL    = os.environ.get("AIRTABLE_DATABASE_TBL", "tblOx4tapKq2a0sBR")
MASTER_TBL = os.environ.get("AIRTABLE_MASTER_TBL", "tbl6FWyYcBfcIq64T")
REG_TBL   = os.environ.get("AIRTABLE_REGISTRY_TBL", "tblT1Hqk2bEC9xVcR")
PROP_TBL  = os.environ.get("AIRTABLE_PROPOSALS_TBL", "Proposals")
SUMMARY_IN = os.environ.get("SUMMARY_IN", "scan-summary.json")
SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", "snapshots")
SHADOW    = os.environ.get("SHADOW", "true").lower() != "false"
DRY_RUN   = os.environ.get("DRY_RUN", "false").lower() == "true"
MAX_ASSESS = int(os.environ.get("MAX_ASSESS", "40"))
ASSESS_LOW = os.environ.get("ASSESS_LOW", "false").lower() == "true"
MAX_PROPOSALS = int(os.environ.get("MAX_PROPOSALS", "50"))

TOKEN = os.environ.get("AIRTABLE_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# Fields the model may propose (brief §4.1). Linked = resolves to Master Variables.
LINKED = {"Pricing Model", "AI / Agentic Capabilities", "MCP Capabilities",
          "Integrations", "Compliance & Security", "SDK Languages", "Use Case Fit"}
SELECT = {"MCP Type": ["Product", "Platform", "None"],
          "Status (lifecycle)": ["Active", "Acquired", "Discontinued", "Sunsetting"]}
NUMERIC = {"Price range (low)", "Price range (high)"}
WHITELIST = sorted(LINKED | set(SELECT) | NUMERIC)

# Master Variables categories that back the linked fields, by their category name.
MASTER_CATEGORIES = {
    "AI / Agentic Capabilities": "AI / Agentic Capabilities",
    "MCP Capabilities": "MCP Capabilities",
    "Pricing Model": "Pricing Model",
    "SDK Languages": "SDK Languages",
    "Integrations": "Integrations",
    "Data Warehouse Support": "Data Warehouse Support",
    "Compliance & Security": "Compliance & Security",
    "Use Case Fit": "Use Case Fit",
}


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s or "x"


def url_slug(url: str) -> str:
    p = urllib.parse.urlparse(url)
    base = (p.path or "").strip("/") or "home"
    return _slug(base)[:70]


def snapshot_path(vendor_slug: str, url_type: str, url: str) -> str:
    import hashlib
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:6]
    vs = _slug(vendor_slug) if vendor_slug else "unknown"
    ut = _slug(url_type) if url_type else "other"
    return os.path.join(SNAPSHOT_DIR, vs, f"{ut}--{url_slug(url)}-{h}.txt")


def git_diff(path: str) -> str:
    """Unified diff of the working-tree snapshot vs the committed one (HEAD)."""
    if not os.path.exists(path):
        return ""
    try:
        out = subprocess.run(["git", "diff", "--no-color", "-U3", "HEAD", "--", path],
                             capture_output=True, text=True, timeout=30)
        return out.stdout
    except Exception as e:
        print(f"  git diff failed for {path}: {e}", file=sys.stderr)
        return ""


def commit_url(path: str) -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        repo = os.environ.get("GITHUB_REPOSITORY", "speerotools/testing-tools-data")
        return f"https://github.com/{repo}/commit/{sha}#{path}" if sha else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Gemini (native structured output)
# ---------------------------------------------------------------------------

PROPOSAL_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "field": {"type": "STRING", "enum": WHITELIST},
            "current_value": {"type": "STRING"},
            "proposed_value": {"type": "STRING"},
            "evidence_quote": {"type": "STRING"},
            "source_url": {"type": "STRING"},
            "confidence": {"type": "NUMBER"},
            "rationale": {"type": "STRING"},
            "change_type": {"type": "STRING", "enum": ["vendor_change", "baseline_correction"]},
            "proposal_type": {"type": "STRING", "enum": ["field_change", "new_canonical_option"]},
        },
        "required": ["field", "proposed_value", "evidence_quote", "source_url",
                     "confidence", "rationale", "change_type", "proposal_type"],
    },
}

PROMPT = """You audit a single vendor page for a CRO tools comparison database. A monitored page changed; below is the unified diff of its normalized text, the vendor's current database values for the fields you may touch, and the allowed options for each linked field.

Return proposed field changes as JSON matching the schema. Follow these rules exactly:

1. ONLY propose these fields: {whitelist}. Never anything else.
2. Every proposal MUST carry a verbatim quote from the NEW page text (the '+' side of the diff). No quote, no proposal.
3. Linked/select fields must use an EXISTING allowed option (listed below). If no option fits, set proposal_type="new_canonical_option" and put the suggested new option name in proposed_value — never pick a nearest-wrong tag.
4. "No change" is the expected answer. Marketing-copy rewrites are NOT changes. Return [] when nothing factual changed.
5. Distinguish assistive AI (human-approves) from autonomous/agentic. A chat sidebar or copilot is NOT agentic experimentation.
6. change_type: "vendor_change" if the vendor shipped/altered something; "baseline_correction" if our record was simply wrong all along.
7. Status (lifecycle): only Active/Acquired/Discontinued/Sunsetting, and ONLY with vendor- or acquirer-owned evidence (a sunset notice, acquisition release, a page that stopped selling). A 404 or dead page is NEVER grounds for Discontinued.
8. Price range fields are a 1-5 proxy, not currency; only propose them if the diff clearly moves the tier.

VENDOR: {vendor}
URL ({url_type}): {url}

CURRENT DATABASE VALUES:
{current}

ALLOWED OPTIONS FOR LINKED FIELDS:
{options}

UNIFIED DIFF:
{diff}
"""


def gemini_assess(prompt: str) -> list[dict]:
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": PROPOSAL_SCHEMA,
            "temperature": 0,
        },
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode())
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text) or []
    except Exception as e:
        print(f"  Gemini parse error: {e} | {json.dumps(data)[:400]}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Airtable context
# ---------------------------------------------------------------------------

def load_context():
    api = Api(TOKEN)
    base = api.base(BASE_ID)
    # Master Variables options grouped by category (Name + Category fields, by name).
    options: dict[str, list[str]] = {}
    for r in base.table(MASTER_TBL).all():
        f = r["fields"]
        name = f.get("Name") or ""
        cat = f.get("Category") or f.get("Type") or ""
        if name and cat:
            options.setdefault(cat, []).append(name)
    # Database records with the whitelisted values, by record id (read by name).
    db = {}
    for r in base.table(DB_TBL).all():
        db[r["id"]] = r["fields"]
    # Registry: url -> (vendor_rid, url_type)
    reg = {}
    for r in base.table(REG_TBL).all():
        f = r["fields"]
        u = f.get("URL")
        if not u or not f.get("Active"):
            continue
        vlink = f.get("Vendor") or []
        rid = (vlink[0] if vlink and isinstance(vlink[0], str) else (vlink[0].get("id") if vlink else "")) if vlink else ""
        reg[u] = (rid, f.get("URL Type") or "")
    return base, options, db, reg


def current_values(fields: dict) -> str:
    out = []
    for k in WHITELIST:
        v = fields.get(k)
        if isinstance(v, list):
            v = ", ".join(x.get("name", x) if isinstance(x, dict) else str(x) for x in v)
        out.append(f"- {k}: {v if v not in (None, '') else '(empty)'}")
    return "\n".join(out)


def options_block(options: dict) -> str:
    out = []
    for field, cat in MASTER_CATEGORIES.items():
        opts = options.get(cat) or options.get(field) or []
        if field in WHITELIST or field == "Data Warehouse Support":
            out.append(f"- {field}: {', '.join(sorted(opts)) or '(none)'}")
    out.append("- MCP Type: Product, Platform, None")
    out.append("- Status (lifecycle): Active, Acquired, Discontinued, Sunsetting")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    if not GEMINI_KEY or not TOKEN:
        print("ERROR: GEMINI_API_KEY and AIRTABLE_TOKEN required", file=sys.stderr)
        sys.exit(1)
    try:
        summary = json.load(open(SUMMARY_IN))
    except FileNotFoundError:
        print(f"No {SUMMARY_IN}; nothing to assess.")
        return
    sevs = ("high", "normal", "low") if ASSESS_LOW else ("high", "normal")
    changed = []
    for sev in sevs:
        changed += summary.get("buckets", {}).get(sev, [])
    if not changed:
        print("No content changes to assess.")
        return
    # Flood guard: never fan out Gemini calls over a noisy scan. Fail loud (§4.6).
    if len(changed) > MAX_ASSESS:
        print(f"ERROR: {len(changed)} changed URLs exceed MAX_ASSESS={MAX_ASSESS}. "
              f"Likely scan noise, not real change — aborting before any Gemini call. "
              f"Investigate the scan or raise MAX_ASSESS deliberately.", file=sys.stderr)
        sys.exit(1)

    if DRY_RUN:
        print(f"DRY_RUN: {len(changed)} URLs would be assessed (no Gemini calls):")
        for it in changed:
            print(f"  {it.get('type','')}  {it.get('url')}")
        return

    print(f"Assessing {len(changed)} changed URLs (shadow={SHADOW}, model={GEMINI_MODEL}).")
    base, options, db, reg = load_context()
    names = {rid: f.get("Name", rid) for rid, f in db.items()}
    slugs = {rid: (f.get("Slug") or _slug(f.get("Name", ""))) for rid, f in db.items()}
    opts_txt = options_block(options)

    proposals: list[dict] = []
    for item in changed:
        url = item.get("url")
        rid, url_type = reg.get(url, ("", item.get("type") or ""))
        if not rid or rid not in db:
            continue
        path = snapshot_path(slugs.get(rid, ""), url_type, url)
        diff = git_diff(path)
        if not diff.strip():
            continue
        prompt = PROMPT.format(
            whitelist=", ".join(WHITELIST), vendor=names.get(rid, "?"),
            url_type=url_type, url=url, current=current_values(db[rid]),
            options=opts_txt, diff=diff[:12000])
        try:
            raw = gemini_assess(prompt)
        except Exception as e:
            print(f"  Gemini call failed for {url}: {e}", file=sys.stderr)
            continue
        for p in raw:
            if p.get("field") not in WHITELIST:
                continue
            if not (p.get("evidence_quote") and p.get("source_url")):
                continue  # evidence mandatory (§4.3)
            p["record_id"] = rid
            p["vendor"] = names.get(rid, "?")
            p["diff_link"] = commit_url(path)
            proposals.append(p)

    if len(proposals) > MAX_PROPOSALS:
        print(f"ERROR: {len(proposals)} proposals exceed cap {MAX_PROPOSALS}; aborting without writing.", file=sys.stderr)
        sys.exit(1)

    print(f"{len(proposals)} proposals generated.")
    for p in proposals:
        print(f"  {p['vendor']} · {p['field']}: {p.get('current_value','')} -> {p['proposed_value']} "
              f"({p['change_type']}, conf {p.get('confidence')})")

    if SHADOW:
        json.dump(proposals, open("proposals.json", "w"), indent=2)
        print("SHADOW mode: wrote proposals.json, did NOT create Airtable rows.")
        return

    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tbl = base.table(PROP_TBL)
    created = 0
    for p in proposals:
        row = {
            "Vendor": [p["record_id"]],
            "Target Field": p["field"],
            "Current Value": str(p.get("current_value", "")),
            "Proposed Value": str(p["proposed_value"]),
            "Evidence Quote": p["evidence_quote"],
            "Source URL": p["source_url"],
            "Confidence": float(p.get("confidence") or 0),
            "Rationale": p.get("rationale", ""),
            "Change Type": p.get("change_type", "vendor_change"),
            "Scan Date": today,
            "Diff Link": p.get("diff_link", ""),
            "Status": "Pending",
        }
        tbl.create(row)
        created += 1
    print(f"Created {created} Pending proposals in {PROP_TBL}.")


if __name__ == "__main__":
    main()
