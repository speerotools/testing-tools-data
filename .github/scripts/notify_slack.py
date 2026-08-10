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
REVIEW_URL = os.environ.get("REVIEW_URL", "")

LABEL = {"high": "HIGH — pricing", "normal": "CHANGED — needs review",
         "low": "LOW — marketing", "error": "ERRORS", "noisy": "NOISY"}


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
        print(f"No {SUMMARY_IN}; nothing to post.")
        return
    text = build_text(summary)
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
