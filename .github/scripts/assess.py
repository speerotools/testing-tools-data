#!/usr/bin/env python3
"""
assess.py: P2 of the Proposal Queue. Turn snapshot diffs into reviewable
Airtable change proposals. Writes NOTHING to the vendor Database table; it only
creates Pending rows in the `Proposals` table for a human to approve (apply.py
does the writing).

git is the review ledger. Runs after enrich.py, BEFORE the snapshots are
committed: HEAD holds the last reviewed snapshot of every page, the working tree
holds this run's fetch, so every tracked snapshot that differs from HEAD is a page
with an unreviewed change, however many runs ago it changed, and every untracked
one is a first sighting. Airtable's content hashes (scan-summary.json buckets)
only drive the Slack triage; they advance on every fetch, so they cannot say what
is still unreviewed.

Pipeline: changed snapshot -> registry entry (vendor, URL, type) -> unified diff
+ current record (linked values as names) + allowed options -> Claude Opus 5.5
(structured output) -> validated proposals -> Proposals rows.

Guardrails (brief §4, §8, §11):
  - Field whitelist (WHITELIST). Anything else is dropped here, and re-checked
    again in apply.py: a schema constrains shape, not truthfulness.
  - Every proposal needs an evidence quote that is actually on the fetched page;
    the source URL is always the page itself.
  - Nothing is lost: a page's snapshot only advances once its facts reached the
    queue. Pages over a cap, failed calls, dry/shadow runs go in
    hold-snapshots.txt, the workflow leaves those snapshots uncommitted, and the
    next run assesses them again (dedup keeps already-queued facts out).
  - Status (lifecycle): only Active/Acquired/Discontinued/Sunsetting, and a
    404/dead URL never justifies Discontinued; enforced in the prompt and in
    apply.py.
  - Linked fields snap to existing Master Variables options, else the proposal
    is a new_canonical_option for human review.

Environment:
  ANTHROPIC_API_KEY required
  ANTHROPIC_MODEL   default: claude-opus-5-5
  ANTHROPIC_EFFORT  default: medium (low|medium|high|xhigh|max; lower = cheaper)
  AIRTABLE_TOKEN   required (read Database/Master/registry, write Proposals)
  AIRTABLE_BASE_ID default: appRX3rtuXifUnvD4
  SUMMARY_IN       default: scan-summary.json
  SHADOW           default: "true". Call Claude, write proposals.json, but do
                   NOT create Airtable rows (and hold every snapshot: preview).
  DRY_RUN          default: "false". List changed URLs + diff sizes and exit
                   WITHOUT calling Claude (free; holds every snapshot).
  MAX_ASSESS       default: 300. Pages assessed per run, largest diffs first;
                   the rest are held for the next run, not lost.
  ASSESS_CONCURRENCY default: 4 parallel Claude calls.
  ASSESS_LOW       default: "false". Skip the low/marketing-homepage bucket
                   (churniest, near-zero factual yield) to save Claude calls.
  PER_VENDOR_CAP   default: 6 proposals per vendor per run (rest held).
  MAX_PROPOSALS    default: 150 per run (rest held).

Outputs (repo root): proposals.json (new proposals, for the Slack digest),
assess-status.json (pages assessed/failed/held, tokens, estimated cost), and
hold-snapshots.txt (snapshots the workflow must not commit).

Cost note: one Claude call per changed page. The 2026-11-01 scan had ~250
changed pages, so a full month is a few hundred calls; the real token count and
cost of every run are printed and posted to Slack. Levers: ANTHROPIC_EFFORT=low,
a cheaper ANTHROPIC_MODEL (claude-sonnet-5-5), or a lower MAX_ASSESS.
"""

from __future__ import annotations
import json
import os
import re
import subprocess
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

try:
    from pyairtable import Api
except ImportError:
    print("ERROR: pip install 'pyairtable>=3,<4'", file=sys.stderr)
    sys.exit(1)

try:
    import anthropic
except ImportError:
    print("ERROR: pip install anthropic", file=sys.stderr)
    sys.exit(1)

BASE_ID   = os.environ.get("AIRTABLE_BASE_ID", "appRX3rtuXifUnvD4")
DB_TBL    = os.environ.get("AIRTABLE_DATABASE_TBL", "tblOx4tapKq2a0sBR")
MASTER_TBL = os.environ.get("AIRTABLE_MASTER_TBL", "tbl6FWyYcBfcIq64T")
REG_TBL   = os.environ.get("AIRTABLE_REGISTRY_TBL", "tblT1Hqk2bEC9xVcR")
PROP_TBL  = os.environ.get("AIRTABLE_PROPOSALS_TBL", "Proposals")
SUMMARY_IN = os.environ.get("SUMMARY_IN", "scan-summary.json")
SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", "snapshots")
# Preview unless explicitly "false" (stray spaces or quotes typed into the GitHub
# variable are ignored, so ' false' or '"false"' still turn it off).
SHADOW    = os.environ.get("SHADOW", "true").strip().strip("'\"").strip().lower() != "false"
DRY_RUN   = os.environ.get("DRY_RUN", "false").lower() == "true"
MAX_ASSESS = int(os.environ.get("MAX_ASSESS") or "300")   # pages per run; overflow is held, not lost
MAX_PER_PAGE = 4                                             # hard cap per page (schema can't express it)
MAX_DIFF_CHARS = int(os.environ.get("MAX_DIFF_CHARS") or "40000")  # longer diffs keep added lines first
ASSESS_CONCURRENCY = int(os.environ.get("ASSESS_CONCURRENCY") or "4")
ASSESS_LOW = os.environ.get("ASSESS_LOW", "false").lower() == "true"
MAX_PROPOSALS = int(os.environ.get("MAX_PROPOSALS") or "150")
PER_VENDOR_CAP = int(os.environ.get("PER_VENDOR_CAP") or "6")
# Short but real facts exist ("Datadog is SOC 2 Type II certified." is 35); the quote
# must also be found on the page, so 25 still screens out nav labels and headlines.
MIN_EVIDENCE_LEN = int(os.environ.get("MIN_EVIDENCE_LEN") or "25")
MIN_ADDED = int(os.environ.get("MIN_ADDED", "2"))  # min GENUINELY-new content lines to bother assessing
# URL types that are narrative/marketing/dynamic; facts belong on docs/pricing/
# product/trust pages, not here. Mining these for capability tags is where the
# noise comes from (brief appendix, design point 1). Skip them for assessment.
ASSESS_SKIP_TYPES = {t.strip().lower() for t in os.environ.get(
    "ASSESS_SKIP_TYPES",
    "Blog/Announcements,Sitemap,Homepage,Solutions/Customers,Press").split(",") if t.strip()}

TOKEN = os.environ.get("AIRTABLE_TOKEN")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")
ANTHROPIC_EFFORT = os.environ.get("ANTHROPIC_EFFORT", "medium")

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
        out = subprocess.run(["git", "diff", "--no-color", "--text", "-U3", "HEAD", "--", path],
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


def history_url(path: str) -> str:
    """GitHub history of one snapshot file. Its newest commit is the scan that
    changed the page, so the reviewer lands on the actual diff. (Assess runs before
    that commit exists, so linking HEAD would show the old, pre-change page.)"""
    repo = os.environ.get("GITHUB_REPOSITORY", "speerotools/testing-tools-data")
    return f"https://github.com/{repo}/commits/main/{path}"


def write_outputs(proposals: list, status: dict, hold: list) -> None:
    """proposals.json feeds the Slack digest; assess-status.json carries counts and
    cost; hold-snapshots.txt lists snapshots the workflow must NOT commit (failed,
    deferred, or dry-run pages) so those changes are re-assessed next run."""
    json.dump(proposals, open("proposals.json", "w"), indent=2)
    json.dump(status, open("assess-status.json", "w"), indent=2)
    with open("hold-snapshots.txt", "w") as fh:
        fh.write("".join(f"{p}\n" for p in sorted(set(hold))))


# ---------------------------------------------------------------------------
# Claude (structured output)
# ---------------------------------------------------------------------------

# Anthropic structured-output schema (output_config.format). Top level must be an
# object, so the proposals ride in a "proposals" array. Only the documented subset
# is allowed: array constraints beyond minItems 0/1 (maxItems etc.), numeric and
# string bounds all return a 400, and every object needs additionalProperties
# false. That 400 is what silently emptied the 2026-11-01 run, so the per-page cap
# lives in code (MAX_PER_PAGE) instead of the schema.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": WHITELIST},
                    "current_value": {"type": "string"},
                    "proposed_value": {"type": "string"},
                    "evidence_quote": {"type": "string"},
                    "source_url": {"type": "string"},
                    "confidence": {"type": "number"},
                    "rationale": {"type": "string"},
                    "change_type": {"type": "string", "enum": ["vendor_change", "baseline_correction"]},
                    "proposal_type": {"type": "string", "enum": ["field_change", "new_canonical_option"]},
                },
                "required": ["field", "current_value", "proposed_value", "evidence_quote",
                             "source_url", "confidence", "rationale", "change_type", "proposal_type"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["proposals"],
    "additionalProperties": False,
}

# The rules and the allowed options are identical for every page in a run, so they
# form a cached system prompt; only the per-page details go in the user message.
SYSTEM_PROMPT = """You maintain a factual database of A/B-testing and experimentation vendors. A monitored page changed; the user message gives the unified diff of its normalized text and the vendor's current database values, and the allowed options for each linked field are listed below. Propose field changes ONLY when the page states a real, verified product fact that the database is missing or has wrong.

Return JSON matching the schema: an object with a "proposals" array. Default to an empty proposals array; most diffs are copy churn and the correct answer is no proposals.

RULES (follow exactly):
1. ONLY these fields: {whitelist}. Nothing else.
2. Evidence: every proposal needs a verbatim quote from the NEW ('+') text that, on its own, factually states the specific thing you are proposing. A headline, section title, nav label, list heading, blog title, or a bare noun/phrase (e.g. "Automation", "Deal Room", "Sales", "AI Summary:") is NOT evidence; reject it. If you cannot quote a full sentence that names the capability/integration/SDK as a supported product feature, do not propose it.
3. Linked fields (Pricing Model, AI / Agentic Capabilities, MCP Capabilities, Integrations, Compliance & Security, SDK Languages, Use Case Fit): the proposed_value MUST be one of the EXISTING allowed options listed below, copied verbatim. If the fact is real but no option fits, set proposal_type="new_canonical_option" with the suggested name; never coerce a marketing phrase into a tag. A linked proposal ADDS one option and never removes one: if an option on our record is wrong, propose the right one and name the wrong one in the rationale so the reviewer can remove it by hand.
4. This is a marketing page vs a product/docs page distinction: blog posts, press releases, and homepages describe narratives and aspirations. Do NOT extract capability/integration/SDK tags from them. Trust docs, pricing, product-feature, API, and trust/security pages for facts.
5. MCP Capabilities are specific MCP-server tools/verbs (e.g. "Write: create experiment", "Read: metrics"). Product features, campaign types, or asset types are NOT MCP capabilities. If a page shows an MCP server exists but no listed capability fits, propose MCP Type instead and leave MCP Capabilities alone.
6. Assistive vs autonomous: a copilot, chat sidebar, or "AI that drafts for human approval" is assistive, NOT agentic experimentation. Do not tag agentic capabilities for assistive features.
7. change_type: "vendor_change" if the vendor shipped/changed something; "baseline_correction" if our record was simply always wrong.
8. Status (lifecycle): only Active/Acquired/Discontinued/Sunsetting, and ONLY with vendor- or acquirer-owned evidence (sunset notice, acquisition release, a page that stopped selling). A 404/dead page is NEVER grounds for Discontinued.
9. Price range (low/high) is a 1-5 proxy, not currency; propose only if the diff clearly moves the tier.
10. Confidence: calibrate honestly. Docs/pricing/trust evidence that explicitly names the fact = high (0.85+). Anything inferred, or from a blog/marketing page = low (below 0.6). Do not return 1.0 unless the quote is unambiguous and from an authoritative page.
11. HARD LIMIT: propose at most 3 changes for this page, the most significant genuinely-new facts only. NEVER enumerate a list. If a docs/integration/SDK page lists many items, do not turn each into a proposal, that is the page's normal catalog, not a change. If you find yourself wanting to propose more than 3, the page is a list and the right answer is the 1-2 most important truly-new additions, or an empty proposals array.

12. These are NOT evidence of a capability, integration or SDK: a row of a pricing or plan comparison table (a feature name followed by Included / Not included / Add-on marks), a list of AI clients or IDEs that can connect to an MCP server (Cursor, Claude Code, VS Code, Windsurf, ...), a docs index or changelog line that only announces a documentation topic ("Publishes a topic on ..."), and features of a different product from the same company than the one this vendor record covers.
13. Never propose a value the vendor already has: check CURRENT DATABASE VALUES first.

ALLOWED OPTIONS FOR LINKED FIELDS (use these verbatim):
{options}
"""

USER_PROMPT = """VENDOR: {vendor}
URL (type: {url_type}): {url}
{page_note}
CURRENT DATABASE VALUES:
{current}

UNIFIED DIFF:
{diff}
"""

# USD per million tokens: input, output, cache read, cache write (5-minute TTL =
# 1.25x input). Only models with documented prices; others report no estimate.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
}


def estimate_cost(tokens: dict) -> float | None:
    p = PRICES.get(ANTHROPIC_MODEL)
    if not p:
        return None
    return round((tokens["input"] * p[0] + tokens["output"] * p[1]
                  + tokens["cache_read"] * p[2] + tokens["cache_write"] * p[3]) / 1e6, 4)


_client = None


def _anthropic():
    """Lazily build the Anthropic client (reads ANTHROPIC_API_KEY). The SDK retries
    connection errors, 408/409/429 and 5xx itself with backoff."""
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, max_retries=3)
    return _client


def claude_assess(system: str, user: str) -> tuple[str, list[dict], dict, str]:
    """Assess one changed page. Returns (state, proposals, usage, error):
      "ok"      proposals is a list, possibly empty (most diffs are churn)
      "refused" the model declined; treated as assessed, nothing proposed
      "failed"  no usable answer (API error, truncation, bad output); the caller
                holds this page's snapshot back so it is retried next run.
    Structured outputs replace Gemini's responseSchema. On Opus 5.5 thinking is
    always on (so `thinking` is omitted), and temperature and forced tool_choice
    are rejected, so a json_schema format block is how the proposals come back."""
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    try:
        resp = _anthropic().messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=16000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={
                "effort": ANTHROPIC_EFFORT,
                "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA},
            },
        )
    except anthropic.APIStatusError as e:
        return "failed", [], usage, f"API {e.status_code}: {str(e)[:300]}"
    except Exception as e:  # connection/timeout after the SDK's retries, or a bug
        return "failed", [], usage, f"{type(e).__name__}: {str(e)[:300]}"
    u = resp.usage
    usage = {"input": u.input_tokens or 0, "output": u.output_tokens or 0,
             "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
             "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0}
    if resp.stop_reason == "refusal":
        return "refused", [], usage, f"refusal: {getattr(resp, 'stop_details', None)}"
    if resp.stop_reason == "max_tokens":
        return "failed", [], usage, "output truncated at max_tokens"
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        props = (json.loads(text) if text else {}).get("proposals") or []
    except (ValueError, AttributeError) as e:
        return "failed", [], usage, f"unparseable output: {e}"
    return "ok", props, usage, ""


# ---------------------------------------------------------------------------
# Airtable context
# ---------------------------------------------------------------------------

def load_context():
    """Everything assess needs from Airtable, read by field name. Returns
    (base, options, option_names, db, reg):
      options       category -> [option names], the allowed values per linked field
      option_names  Master Variables record id -> name. Linked fields on the
                    Database come back as record ids; the model and the reviewer
                    must see names, or "what the vendor already has" is unreadable.
      reg           registry rows (url, vendor record id, url type). A list, not a
                    url-keyed dict: one URL can be registered for two vendors and
                    each gets its own snapshot file."""
    api = Api(TOKEN)
    base = api.base(BASE_ID)
    options: dict[str, list[str]] = {}
    option_names: dict[str, str] = {}
    for r in base.table(MASTER_TBL).all():
        f = r["fields"]
        name = str(f.get("Name") or "").strip()
        # The category is the "Variable" field (as in the canonical sync/extract.py).
        cat = f.get("Variable") or f.get("Category") or f.get("Type") or ""
        cat = str(cat.get("name", "") if isinstance(cat, dict) else cat).strip()
        if name:
            option_names[r["id"]] = name
            if cat:
                options.setdefault(cat, []).append(name)
    db = {r["id"]: r["fields"] for r in base.table(DB_TBL).all()}
    reg = []
    for r in base.table(REG_TBL).all():
        f = r["fields"]
        u = f.get("URL")
        if not u or not f.get("Active"):
            continue
        vlink = f.get("Vendor") or []
        rid = (vlink[0] if isinstance(vlink[0], str) else vlink[0].get("id", "")) if vlink else ""
        reg.append((u, rid, f.get("URL Type") or ""))
    return base, options, option_names, db, reg


def current_values(fields: dict, option_names: dict) -> str:
    """The vendor's whitelisted values as readable text (linked ids -> names)."""
    out = []
    for k in WHITELIST:
        v = fields.get(k)
        if isinstance(v, list):
            v = ", ".join(sorted(option_names.get(x, x) if isinstance(x, str)
                                 else (x.get("name") or option_names.get(x.get("id"), "?")) for x in v))
        out.append(f"- {k}: {v if v not in (None, '') else '(empty)'}")
    return "\n".join(out)


def git_changed_snapshots() -> tuple[list[str], list[str]]:
    """git is the review ledger. HEAD holds the last REVIEWED snapshot of every
    page and the working tree holds this run's fetch, so a tracked file that
    differs is a page with an unreviewed change, however many runs ago it
    happened; an untracked file is a page seen for the first time. (Airtable's
    content hashes advance on every fetch and only drive the Slack triage, so they
    cannot tell what is still unreviewed.)"""
    def run(*args):
        out = subprocess.run(["git", *args], capture_output=True, text=True, timeout=120, check=True).stdout
        return [p for p in out.split("\0") if p]
    tracked = run("diff", "--name-only", "-z", "HEAD", "--", SNAPSHOT_DIR)
    new = run("ls-files", "--others", "--exclude-standard", "-z", "--", SNAPSHOT_DIR)
    return tracked, new


def new_page_diff(path: str) -> str:
    """A first-sighting page as a diff in which every line is new."""
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return ""
    return f"--- /dev/null\n+++ b/{path}\n" + "\n".join("+" + ln for ln in lines)


def fit_diff(diff: str, limit: int) -> tuple[str, bool]:
    """Keep a diff under `limit` chars. Over the limit, added lines go first (new
    facts live there), so a huge reflow never hides them. Returns (text, cut)."""
    if len(diff) <= limit:
        return diff, False
    lines = diff.splitlines()
    out = [ln for ln in lines[:2] if ln.startswith(("---", "+++"))]
    size = sum(len(ln) + 1 for ln in out)
    for ln in lines:
        if ln.startswith("+") and not ln.startswith("+++"):
            if size + len(ln) + 1 > limit:
                break
            out.append(ln)
            size += len(ln) + 1
    out.append("[diff shortened: only added lines are shown]")
    return "\n".join(out), True


_QUOTE_FOLD = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                             "\u2013": "-", "\u2014": "-", "\u00a0": " "})


def _fold(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().translate(_QUOTE_FOLD)).strip()


def quote_on_page(quote: str, page: str) -> bool:
    """The evidence quote must really be on the fetched page. Case, whitespace and
    quote/dash styles are ignored, and '...' may join excerpts."""
    parts = [p.strip(" .\"'") for p in re.split(r"\.\.\.|…", _fold(quote))]
    parts = [p for p in parts if len(p) >= 12]
    return bool(parts) and all(p in page for p in parts)


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
    if not ANTHROPIC_KEY or not TOKEN:
        print("ERROR: ANTHROPIC_API_KEY and AIRTABLE_TOKEN required", file=sys.stderr)
        sys.exit(1)
    try:
        summary = json.load(open(SUMMARY_IN))
    except (FileNotFoundError, ValueError):
        summary = {}
    scan_date = summary.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    base, options, option_names, db, reg = load_context()
    if not options:
        print("WARNING: no Master Variables options loaded (expected a 'Variable' field); "
              "every linked proposal will be flagged as a new option.", file=sys.stderr)
    names = {rid: f.get("Name", rid) for rid, f in db.items()}
    slugs = {rid: (f.get("Slug") or _slug(f.get("Name", ""))) for rid, f in db.items()}
    opts_txt = options_block(options)
    # field -> {lowercase option -> canonical spelling}
    canon = {field: {o.lower(): o for o in (options.get(cat) or options.get(field) or [])}
             for field, cat in MASTER_CATEGORIES.items()}

    # Every registered (vendor, URL) -> the snapshot file enrich.py writes for it.
    path_map = {}
    for url, rid, url_type in reg:
        if rid in db:
            path_map[snapshot_path(slugs[rid], url_type, url)] = (url, rid, url_type)

    tracked, new = git_changed_snapshots()
    status = {"date": scan_date, "changed_files": len(tracked), "first_sightings": len(new),
              "unmapped": 0, "skipped_type": 0, "gated": 0, "candidates": 0, "assessed": 0,
              "failed": 0, "refused": 0, "deferred": 0, "held_by_caps": 0, "unverified": 0,
              "shortened": 0, "thin_evidence": 0, "proposals": 0, "created": 0, "create_error": "",
              "options_loaded": sum(len(v) for v in options.values()),
              "dry_run": DRY_RUN, "shadow": SHADOW, "model": ANTHROPIC_MODEL,
              "effort": ANTHROPIC_EFFORT, "last_error": "", "est_cost_usd": None,
              "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}}

    cands = []
    for path, is_new in [(p, False) for p in tracked] + [(p, True) for p in new]:
        if path not in path_map:
            status["unmapped"] += 1
            continue
        url, rid, url_type = path_map[path]
        t = url_type.strip().lower()
        if t in ASSESS_SKIP_TYPES or (not ASSESS_LOW and any(k in t for k in ("marketing", "home", "landing"))):
            status["skipped_type"] += 1
            continue
        if not os.path.exists(path):
            continue
        diff = new_page_diff(path) if is_new else git_diff(path)
        if not diff.strip():
            continue
        added = diff_new_lines(diff)
        # Diff-gate: deletions, reorders and re-fetch churn are noise. One new line
        # is enough on a pricing page (a single changed price is a real change).
        if added < (1 if "pricing" in t else MIN_ADDED):
            status["gated"] += 1
            continue
        cands.append({"url": url, "rid": rid, "url_type": url_type, "path": path,
                      "diff": diff, "added": added, "new": is_new})
    print(f"{len(tracked)} changed + {len(new)} new snapshot(s): {status['skipped_type']} marketing/dynamic, "
          f"{status['gated']} noise, {status['unmapped']} not in the registry, {len(cands)} to assess.")

    # Nothing is lost: pages not assessed this run (over the cap, failed, dry run,
    # shadow, or cut by a proposal cap) go in `hold`; the workflow leaves those
    # snapshots uncommitted, so git still shows the change next run.
    # Pricing pages first (one changed price matters most), then the most new content.
    cands.sort(key=lambda c: ("pricing" not in c["url_type"].lower(), -c["added"]))
    deferred, cands = cands[MAX_ASSESS:], cands[:MAX_ASSESS]
    status.update(candidates=len(cands) + len(deferred), deferred=len(deferred))
    hold = [c["path"] for c in deferred]
    if deferred:
        print(f"Assessing the top {MAX_ASSESS} (pricing first, then most added); "
              f"holding {len(deferred)} for the next run.")
    print(f"Assessing {len(cands)} page(s) (shadow={SHADOW}, model={ANTHROPIC_MODEL}, effort={ANTHROPIC_EFFORT}).")

    if DRY_RUN:
        for c in cands:
            print(f"  +{c['added']}  {'NEW ' if c['new'] else ''}{c['url_type']}  {c['url']}")
        write_outputs([], status, hold + [c["path"] for c in cands])
        return

    system = SYSTEM_PROMPT.format(whitelist=", ".join(WHITELIST), options=opts_txt)

    def run(item):
        diff, item["shortened"] = fit_diff(item["diff"], MAX_DIFF_CHARS)
        note = ("PAGE STATUS: first sighting. This URL was just added to monitoring, so the whole "
                "page shows as new. Propose only facts the database is missing.\n" if item["new"] else "")
        user = USER_PROMPT.format(vendor=names.get(item["rid"], "?"), url_type=item["url_type"],
                                  url=item["url"], page_note=note,
                                  current=current_values(db[item["rid"]], option_names), diff=diff)
        return claude_assess(system, user)

    # One call first so it writes the prompt cache; the rest then read it in parallel.
    results = [run(cands[0])] if cands else []
    if len(cands) > 1:
        with ThreadPoolExecutor(max_workers=max(1, ASSESS_CONCURRENCY)) as pool:
            results += list(pool.map(run, cands[1:]))

    dropped = {"evidence": 0, "noncanonical": 0}
    proposals: list[dict] = []
    for item, (state, raw, usage, err) in zip(cands, results):
        for k, v in usage.items():
            status["tokens"][k] += v
        if state == "failed":
            status["failed"] += 1
            status["last_error"] = err
            hold.append(item["path"])
            print(f"  Claude failed for {item['url']}: {err}", file=sys.stderr)
            continue
        status["assessed"] += 1
        status["shortened"] += int(bool(item.get("shortened")))
        if state == "refused":
            status["refused"] += 1
            print(f"  Claude declined {item['url']}: {err}", file=sys.stderr)
            continue
        rid, path = item["rid"], item["path"]
        try:
            page = _fold(open(path, encoding="utf-8", errors="replace").read())
        except OSError:
            page = ""
        for p in raw:
            if p.get("field") not in WHITELIST:
                continue
            quote = (p.get("evidence_quote") or "").strip()
            # Thin quotes (headlines, nav labels, bare nouns) are the main noise source.
            if len(quote) < MIN_EVIDENCE_LEN:
                dropped["evidence"] += 1
                continue
            # The quote must really be on the page; a paraphrase is not evidence.
            if not quote_on_page(quote, page):
                status["unverified"] += 1
                continue
            # Linked values snap to the canonical Master Variables spelling; anything
            # else is at most a new-canonical suggestion, never a silent field write.
            opts = canon.get(p["field"])
            if opts is not None:
                hit = opts.get(str(p.get("proposed_value", "")).strip().lower())
                if hit:
                    p["proposed_value"] = hit
                    p["proposal_type"] = "field_change"
                elif p.get("proposal_type") != "new_canonical_option":
                    p["proposal_type"] = "new_canonical_option"
                    dropped["noncanonical"] += 1
            p["source_url"] = item["url"]  # the page we fetched, not the model's claim
            p["rationale"] = re.sub(r"\s*\u2014\s*", ", ", str(p.get("rationale") or ""))
            p["record_id"] = rid
            p["vendor"] = names.get(rid, "?")
            p["diff_link"] = history_url(path)
            p["_path"] = path
            p["linked"] = p["field"] in LINKED  # apply adds the option; it never removes one
            proposals.append(p)
    status["thin_evidence"] = dropped["evidence"]
    print(f"Dropped {dropped['evidence']} thin-evidence and {status['unverified']} quote(s) not found on "
          f"the page; flagged {dropped['noncanonical']} non-canonical as new_canonical_option.")

    # Dedup BEFORE the caps, so already-queued facts never use up a cap slot.
    # Pending/Approved/Applied/Rejected rows block a repeat (re-runs never
    # duplicate, rejected ideas stay rejected); Failed/Stale rows do not, so those
    # facts get another try. Repeats within this run are dropped too.
    seen = set()
    try:
        queued = base.table(PROP_TBL).all()
    except Exception as e:  # keep the paid results: write them out, advance nothing
        status["last_error"] = f"reading {PROP_TBL} for dedup failed: {e}"[:300]
        print(f"ERROR: {status['last_error']}. proposals.json keeps this run's results; "
              f"no snapshot advances.", file=sys.stderr)
        for p in proposals:
            p.pop("_path", None)
        write_outputs(proposals, status, hold + [c["path"] for c in cands])
        sys.exit(1)
    for r in queued:
        f = r["fields"]
        if f.get("Status") in ("Failed", "Stale"):
            continue
        seen.add(((f.get("Vendor") or [""])[0], f.get("Target Field"),
                  str(f.get("Proposed Value", "")).strip().lower()))
    fresh = []
    for p in proposals:
        key = (p["record_id"], p["field"], str(p["proposed_value"]).strip().lower())
        if key not in seen:
            seen.add(key)
            fresh.append(p)
    if len(fresh) != len(proposals):
        print(f"Skipped {len(proposals) - len(fresh)} proposal(s) already queued.")

    # Caps pace the reviewer, they do not throw facts away: a page whose proposals
    # get cut is held, so next run re-assesses it and the dedup above lets only the
    # facts that are not queued yet through.
    from collections import defaultdict as _dd

    def capped(groups, cap, label):
        kept, cut = [], []
        for key, ps in groups.items():
            ps.sort(key=lambda x: -(x.get("confidence") or 0))
            if len(ps) > cap:
                print(f"  {label} {key}: {len(ps)} -> {cap}; the rest are held for next run")
            kept += ps[:cap]
            cut += ps[cap:]
        return kept, cut

    by_page = _dd(list)
    for p in fresh:
        by_page[p["_path"]].append(p)
    kept, cut = capped(by_page, MAX_PER_PAGE, "page")
    by_vendor = _dd(list)
    for p in kept:
        by_vendor[p["vendor"]].append(p)
    kept, cut_vendor = capped(by_vendor, PER_VENDOR_CAP, "vendor")
    cut += cut_vendor
    if len(kept) > MAX_PROPOSALS:
        kept.sort(key=lambda p: -(p.get("confidence") or 0))
        print(f"{len(kept)} proposals over cap {MAX_PROPOSALS}; keeping the top {MAX_PROPOSALS}, the rest are held.")
        cut += kept[MAX_PROPOSALS:]
        kept = kept[:MAX_PROPOSALS]
    cut_paths = {p["_path"] for p in cut}
    hold += cut_paths
    status["held_by_caps"] = len(cut_paths)
    proposals = kept
    for p in proposals:
        p.pop("_path", None)
    if SHADOW:  # preview only: nothing reaches the queue, so nothing advances
        hold += [c["path"] for c in cands]
    status["proposals"] = len(proposals)
    status["est_cost_usd"] = estimate_cost(status["tokens"])

    cost = status["est_cost_usd"]
    print(f"Claude: {status['assessed']} assessed, {status['failed']} failed, {status['refused']} declined; "
          f"{len(set(hold))} snapshot(s) held for next run. Tokens {status['tokens']}. "
          f"Est. cost {'$%.2f' % cost if cost is not None else 'n/a'}.")
    print(f"{len(proposals)} new proposal(s).")
    for p in proposals:
        print(f"  {p['vendor']} · {p['field']}: {p.get('current_value', '')} -> {p['proposed_value']} "
              f"({p['change_type']}, conf {p.get('confidence')})")

    write_outputs(proposals, status, hold)
    if cands and status["failed"] == len(cands):
        print(f"ERROR: every Claude call failed ({status['last_error']}). Nothing was assessed; "
              f"all snapshot changes stay pending.", file=sys.stderr)
        sys.exit(1)
    if SHADOW:
        print("SHADOW mode: wrote proposals.json, did NOT create Airtable rows.")
        return

    rows = []
    for p in proposals:
        new_opt = p.get("proposal_type") == "new_canonical_option"
        rows.append({
            # Readable summary in the primary field. New options are flagged: they
            # can only be applied once the option exists in Master Variables.
            "Proposal": ((f"{p['vendor']} · {p['field']}: add {p['proposed_value']}" if p.get("linked") else
                          f"{p['vendor']} · {p['field']}: {p.get('current_value') or '∅'} → {p['proposed_value']}")
                         + ("  [NEW OPTION: add it to Master Variables first]" if new_opt else "")),
            "Vendor": [p["record_id"]],
            "Target Field": p["field"],
            "Current Value": str(p.get("current_value", "")),
            "Proposed Value": str(p["proposed_value"]),
            "Evidence Quote": p["evidence_quote"],
            "Source URL": p["source_url"],
            "Confidence": float(p.get("confidence") or 0),
            "Rationale": p.get("rationale", ""),
            "Change Type": p.get("change_type", "vendor_change"),
            "Scan Date": scan_date,
            "Diff Link": p.get("diff_link", ""),
            "Status": "Pending",
        })
    # Ten at a time so a failure says exactly how many rows exist. A failed write
    # fails the step: no snapshot advances, the next run re-assesses those pages,
    # and the dedup above skips the rows that did get created.
    tbl = base.table(PROP_TBL)
    for i in range(0, len(rows), 10):
        try:
            tbl.batch_create(rows[i:i + 10], typecast=True)
        except Exception as e:
            status["create_error"] = str(e)[:300]
            write_outputs(proposals, status, hold)
            print(f"ERROR: Airtable create failed after {status['created']} of {len(rows)} rows: {e}",
                  file=sys.stderr)
            sys.exit(1)
        status["created"] += len(rows[i:i + 10])
    write_outputs(proposals, status, hold)
    print(f"Created {status['created']} Pending proposals in {PROP_TBL}.")


if __name__ == "__main__":
    main()
