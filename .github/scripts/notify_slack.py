#!/usr/bin/env python3
"""
notify_slack.py — post the monthly scan summary to Slack (brief §5).

Reads scan-summary.json (written by enrich.py) and posts one message to a Slack
incoming webhook. A plain webhook is enough; no bot token or app scopes. No-ops
quietly when SLACK_WEBHOOK_URL is unset, so the workflow step is safe to leave
in before the channel exists. Standard library only.

Environment variables:
  SLACK_WEBHOOK_URL   incoming webhook; if unset, prints the text and exits 0
  SUMMARY_IN          default: scan-summary.json
  REVIEW_URL          optional — link to the changed-pages artifact / PR
"""

from __future__ import annotations
import json
import os
import sys
import urllib.request

WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL")
SUMMARY_IN = os.environ.get("SUMMARY_IN", "scan-summary.json")
PROPOSALS_IN = os.environ.get("PROPOSALS_IN", "proposals.json")
REVIEW_URL = os.environ.get("REVIEW_URL", "")
QUEUE_URL = os.environ.get("QUEUE_URL", "")   # Airtable Pending Review view
BEN = "<@U0GP0BBEW>"                            # brief §11.3 — raw token notifies

LABEL = {"high": "HIGH — pricing", "normal": "CHANGED — needs review",
         "low": "LOW — marketing", "error": "ERRORS", "noisy": "NOISY"}


def build_proposals_text(props: list) -> str:
    """Proposal-queue digest, grouped by vendor (brief §6). Lifecycle proposals
    get a distinct marker so they are never skimmed past."""
    from collections import defaultdict
    by_v = defaultdict(list)
    for p in props:
        by_v[p.get("vendor", "?")].append(p)
    low_conf = sum(1 for p in props if (p.get("confidence") or 0) < 0.7)
    lines = [f"{BEN} *Vendor proposals — {len(props)} pending*"]
    for vendor in sorted(by_v):
        items = by_v[vendor]
        lines.append(f"\n*{vendor}* — {len(items)} proposal(s)")
        for p in items:
            lc = "  ⚠️ LIFECYCLE" if p.get("field") == "Status (lifecycle)" else ""
            lines.append(f"  {p.get('field')}: {p.get('current_value','') or '∅'} → {p.get('proposed_value')}{lc}")
            lines.append(f"    \"{(p.get('evidence_quote') or '')[:160]}\"")
            lines.append(f"    {p.get('source_url','')}  ·  {p.get('change_type','')}  ·  conf {p.get('confidence')}")
    foot = [f"\n{len(props)} pending, {low_conf} below 0.7 confidence."]
    if QUEUE_URL:
        foot.append(f"Review + approve: {QUEUE_URL}")
    return "\n".join(lines + foot)


def build_tail(s: dict) -> str:
    """Errors + noisy summary kept underneath the proposals (brief §6)."""
    c = s.get("counts", {})
    out = []
    for sev in ("error", "noisy"):
        items = s.get("buckets", {}).get(sev, [])
        if not items:
            continue
        out.append(f"\n*{LABEL[sev]}* ({len(items)})")
        for b in items[:10]:
            st = f" ({b['status']})" if b.get("status") and b["status"] >= 300 else ""
            out.append(f"• {b['vendor']} — {b['url']}{st}")
        if len(items) > 10:
            out.append(f"  …and {len(items) - 10} more")
    return "\n".join(out)


def build_text(s: dict) -> str:
    c = s.get("counts", {})
    lines = [f"*Vendor scan — {s.get('date','')}*",
             f"Scanned {s.get('scanned',0)} URLs. "
             f"{len(s.get('changed_vendors',[]))} vendor(s) changed, "
             f"{c.get('error',0)} error(s), {c.get('noisy',0)} noisy."]
    for sev in ("high", "normal", "low", "error", "noisy"):
        items = s.get("buckets", {}).get(sev, [])
        if not items:
            continue
        lines.append(f"\n*{LABEL[sev]}* ({len(items)})")
        for b in items[:15]:
            extra = f" [{b['type']}]" if b.get("type") else ""
            st = f" ({b['status']})" if b.get("status") and b["status"] >= 300 else ""
            lines.append(f"• {b['vendor']} — {b['url']}{extra}{st}")
        if len(items) > 15:
            lines.append(f"  …and {len(items) - 15} more")
    if REVIEW_URL:
        lines.append(f"\n→ Review: {REVIEW_URL}")
    return "\n".join(lines)


def main() -> None:
    try:
        summary = json.load(open(SUMMARY_IN))
    except FileNotFoundError:
        summary = {}
    proposals = []
    try:
        proposals = json.load(open(PROPOSALS_IN)) or []
    except FileNotFoundError:
        pass

    # Proposal queue on top (with Ben's mention), scan tail underneath. Only
    # mention Ben when there are proposals — don't train him to ignore the ping.
    if proposals:
        text = build_proposals_text(proposals) + "\n" + build_tail(summary)
    elif summary:
        text = build_text(summary)
    else:
        print("Nothing to post.")
        return

    if not WEBHOOK:
        print("SLACK_WEBHOOK_URL unset — would have posted:\n")
        print(text)
        return
    req = urllib.request.Request(WEBHOOK, data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        print("Posted scan summary to Slack.")
    except Exception as e:
        print(f"ERROR posting to Slack: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
