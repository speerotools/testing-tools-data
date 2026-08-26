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
PER_VENDOR_CAP = int(os.environ.get("PER_VENDOR_CAP", "8"))
MIN_EVIDENCE_LEN = int(os.environ.get("MIN_EVIDENCE_LEN", "40"))
MIN_ADDED = int(os.environ.get("MIN_ADDED", "2"))  # min GENUINELY-new content lines to bother assessing
# URL types that are narrative/marketing/dynamic — facts belong on docs/pricing/
# product/trust pages, not here. Mining these for capability tags is where the
# noise comes from (brief appendix, design point 1). Skip them for assessment.
ASSESS_SKIP_TYPES = {t.strip().lower() for t in os.environ.get(
    "ASSESS_SKIP_TYPES",
    "Blog/Announcements,Sitemap,Homepage,Solutions/Customers,Press").split(",") if t.strip()}

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


def diff_new_lines(diff: str) -> int:
    """Count GENUINELY new content lines: added lines whose text does not also
    appear on the removed side. Reordered or re-fetched content shows up in both
    + and - (cancels to zero = fetch noise); a real new capability line appears
    only in +. This separates real additions from churn even when the page also
    shrank."""
    added, removed = [], []
    for ln in diff.splitlines():
        if ln.startswith(("+++", "---", "@@")):
            continue
        s = ln[1:].strip()
        if len(s) <= 3:
            continue
        if ln.startswith("+"):
            added.append(s)
        elif ln.startswith("-"):
            removed.append(s)
    rem = set(removed)
    return sum(1 for a in added if a not in rem)


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

PROMPT = """You maintain a factual database of A/B-testing and experimentation vendors. A monitored page changed; below is the unified diff of its normalized text, the vendor's current database values, and the allowed options for each linked field. Propose field changes ONLY when the page states a real, verified product fact that the database is missing or has wrong.

Return JSON matching the schema. Default to returning [] — most diffs are copy churn and the correct answer is no proposals.

RULES (follow exactly):
1. ONLY these fields: {whitelist}. Nothing else.
2. Evidence: every proposal needs a verbatim quote from the NEW ('+') text that, on its own, factually states the specific thing you are proposing. A headline, section title, nav label, list heading, blog title, or a bare noun/phrase (e.g. "Automation", "Deal Room", "Sales", "AI Summary:") is NOT evidence — reject it. If you cannot quote a full sentence that names the capability/integration/SDK as a supported product feature, do not propose it.
3. Linked fields (Pricing Model, AI / Agentic Capabilities, MCP Capabilities, Integrations, Compliance & Security, SDK Languages, Use Case Fit): the proposed_value MUST be one of the EXISTING allowed options listed below, copied verbatim. If the fact is real but no option fits, set proposal_type="new_canonical_option" with the suggested name — never coerce a marketing phrase into a tag.
4. This is a marketing page vs a product/docs page distinction: blog posts, press releases, and homepages describe narratives and aspirations. Do NOT extract capability/integration/SDK tags from them. Trust docs, pricing, product-feature, API, and trust/security pages for facts.
5. MCP Capabilities are specific MCP-server tools/verbs (e.g. "Write: create experiment", "Read: metrics"). Product features, campaign types, or asset types are NOT MCP capabilities. If a page shows an MCP server exists but no listed capability fits, propose MCP Type instead and leave MCP Capabilities alone.
6. Assistive vs autonomous: a copilot, chat sidebar, or "AI that drafts for human approval" is assistive, NOT agentic experimentation. Do not tag agentic capabilities for assistive features.
7. change_type: "vendor_change" if the vendor shipped/changed something; "baseline_correction" if our record was simply always wrong.
8. Status (lifecycle): only Active/Acquired/Discontinued/Sunsetting, and ONLY with vendor- or acquirer-owned evidence (sunset notice, acquisition release, a page that stopped selling). A 404/dead page is NEVER grounds for Discontinued.
9. Price range (low/high) is a 1-5 proxy, not currency; propose only if the diff clearly moves the tier.
10. Confidence: calibrate honestly. Docs/pricing/trust evidence that explicitly names the fact = high (0.85+). Anything inferred, or from a blog/marketing page = low (below 0.6). Do not return 1.0 unless the quote is unambiguous and from an authoritative page.
11. If a single page appears to add many (5+) capabilities, it is almost certainly marketing copy — return [].

VENDOR: {vendor}
URL (type: {url_type}): {url}

CURRENT DATABASE VALUES:
{current}

ALLOWED OPTIONS FOR LINKED FIELDS (use these verbatim):
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

    # Drop marketing/dynamic URL types up front (cost + noise).
    before = len(changed)
    changed = [it for it in changed if (it.get("type") or "").strip().lower() not in ASSESS_SKIP_TYPES]
    if before != len(changed):
        print(f"Skipped {before - len(changed)} marketing/dynamic URLs.")

    base, options, db, reg = load_context()
    names = {rid: f.get("Name", rid) for rid, f in db.items()}
    slugs = {rid: (f.get("Slug") or _slug(f.get("Name", ""))) for rid, f in db.items()}
    opts_txt = options_block(options)
    field_opts = {}
    for field, cat in MASTER_CATEGORIES.items():
        field_opts[field] = {o.lower() for o in (options.get(cat) or options.get(field) or [])}

    # Diff-gate (git is free): keep only URLs whose snapshot ADDED real content.
    # Deletion-heavy / tiny diffs are fetch noise (partial responses, bot walls,
    # the sites' own A/B tests), not vendor changes. This is what stops a noisy
    # scan from ever reaching Gemini.
    cands = []
    for item in changed:
        url = item.get("url")
        rid, url_type = reg.get(url, ("", item.get("type") or ""))
        if not rid or rid not in db:
            continue
        path = snapshot_path(slugs.get(rid, ""), url_type, url)
        diff = git_diff(path)
        if not diff.strip():
            continue
        new = diff_new_lines(diff)
        if new < MIN_ADDED:   # too little genuinely-new content = fetch noise
            continue
        cands.append({"url": url, "rid": rid, "url_type": url_type, "path": path,
                      "diff": diff, "added": new})

    cands.sort(key=lambda c: -c["added"])
    if len(cands) > MAX_ASSESS:
        print(f"{len(cands)} substantive diffs; assessing the top {MAX_ASSESS} by added content, "
              f"deferring {len(cands) - MAX_ASSESS} to keep the Gemini bill bounded.")
        cands = cands[:MAX_ASSESS]
    print(f"Assessing {len(cands)} URLs after diff-gating {before} changed (shadow={SHADOW}, model={GEMINI_MODEL}).")

    if DRY_RUN:
        for c in cands:
            print(f"  +{c['added']}  {c['url_type']}  {c['url']}")
        return

    dropped = {"evidence": 0, "noncanonical": 0}
    proposals: list[dict] = []
    for item in cands:
        url = item["url"]; rid = item["rid"]; url_type = item["url_type"]
        path = item["path"]; diff = item["diff"]
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
            quote = (p.get("evidence_quote") or "").strip()
            if not (quote and p.get("source_url")):
                continue  # evidence mandatory (§4.3)
            # Thin quotes (headlines, nav labels, bare nouns) are the main noise source.
            if len(quote) < MIN_EVIDENCE_LEN:
                dropped["evidence"] += 1
                continue
            # Linked fields must match an existing canonical option; otherwise it is
            # at most a new-canonical suggestion, never a silent field write.
            fo = field_opts.get(p["field"])
            if fo is not None and str(p.get("proposed_value", "")).lower() not in fo:
                if p.get("proposal_type") != "new_canonical_option":
                    p["proposal_type"] = "new_canonical_option"
                    dropped["noncanonical"] += 1
            p["record_id"] = rid
            p["vendor"] = names.get(rid, "?")
            p["diff_link"] = commit_url(path)
            proposals.append(p)

    # Per-vendor cap: one page dumping many tags is almost always marketing noise.
    from collections import defaultdict as _dd
    by_v = _dd(list)
    for p in proposals:
        by_v[p["vendor"]].append(p)
    capped = []
    for v, ps in by_v.items():
        ps.sort(key=lambda x: -(x.get("confidence") or 0))
        if len(ps) > PER_VENDOR_CAP:
            print(f"  capped {v}: {len(ps)} -> {PER_VENDOR_CAP} (likely marketing noise)")
        capped.extend(ps[:PER_VENDOR_CAP])
    proposals = capped
    print(f"Dropped {dropped['evidence']} thin-evidence; flagged {dropped['noncanonical']} non-canonical as new_canonical_option.")

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
