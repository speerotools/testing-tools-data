#!/usr/bin/env python3
"""
notify_slack.py: post the monthly scan summary to Slack (brief §5).

Reads proposals.json + assess-status.json (from assess.py) and scan-summary.json
(from enrich.py) and posts one message to a Slack incoming webhook. A plain
webhook is enough; no bot token or app scopes. Without SLACK_WEBHOOK_URL it
prints the message; inside GitHub Actions it then fails the step, so a missing
secret can never silently stop the reviewer ping. Standard library only.

Layout: mention + headline, the Claude status (failures first), the review link,
then the proposals, then the scan tail. Status and link sit on top because the
message is cut at MAX_CHARS (Slack's text limit is 40,000) and only the list
below them may be cut.

Environment variables:
  SLACK_WEBHOOK_URL   incoming webhook; if unset, prints the text (exit 1 in Actions)
  SUMMARY_IN          default: scan-summary.json
  QUEUE_URL           review link (default: the Airtable Proposals view)
  REVIEW_URL          GitHub Actions run link, shown as "Scan log"
  SLACK_MENTION       reviewer mention, a Slack member id (U0GP0BBEW) or token
                      (<@U0GP0BBEW>); anything else falls back to Ben Labay
  ASSESS_OUTCOME      outcome of the workflow's assess step
  RUN_MODE            enrich.yml mode input (monitor / baseline)
"""

from __future__ import annotations
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict

WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL")
SUMMARY_IN = os.environ.get("SUMMARY_IN", "scan-summary.json")
PROPOSALS_IN = os.environ.get("PROPOSALS_IN", "proposals.json")
STATUS_IN = os.environ.get("STATUS_IN", "assess-status.json")
REVIEW_URL = os.environ.get("REVIEW_URL", "")
# `or`, not a get() default: an unset GitHub variable arrives as an empty string.
QUEUE_URL = (os.environ.get("QUEUE_URL")
             or "https://airtable.com/appRX3rtuXifUnvD4/tblsqMlhnvDrVbifc/viwgR2TrZowcuEcKc?blocks=hide")
DEFAULT_MENTION = "<@U0GP0BBEW>"   # Ben Labay


def mention(raw: str | None) -> str:
    """Only a real Slack token notifies anyone: "@Ben" or "Ben Labay" would post
    as plain text. A bare member id is wrapped; anything else uses the default."""
    raw = (raw or "").strip()
    if re.fullmatch(r"<(@[UW][A-Z0-9]+|!subteam\^[A-Z0-9]+|!here|!channel)>", raw):
        return raw
    if re.fullmatch(r"[UW][A-Z0-9]{6,}", raw):
        return f"<@{raw}>"
    if raw:
        print(f"SLACK_MENTION {raw!r} is not a Slack member id; tagging the default reviewer.", file=sys.stderr)
    return DEFAULT_MENTION


BEN = mention(os.environ.get("SLACK_MENTION"))
MAX_CHARS = 30000

LABEL = {"high": "HIGH: pricing", "normal": "CHANGED: needs review",
         "low": "LOW: marketing", "error": "ERRORS", "noisy": "NOISY"}


def esc(s) -> str:
    """Slack mrkdwn needs &, < and > escaped in vendor-supplied text, or it breaks
    the formatting (and text like <!channel> would ping people)."""
    return str("" if s is None else s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def load(path, default):
    try:
        return json.load(open(path))
    except (FileNotFoundError, ValueError):
        return default


def status_lines(st: dict, outcome: str, mode: str) -> tuple[list[str], bool]:
    """Claude run summary as (lines, alarm). Failures come first and loud, so a
    broken assessment can never pass for a quiet month (the 2026-11-01 run failed
    every call and still posted a normal-looking digest)."""
    out, alarm = [], False
    if st.get("create_error"):
        alarm = True
        out.append(f"⚠️ *Airtable rejected the proposals: {st.get('created', 0)} of {st.get('proposals', '?')} "
                   f"rows were created.* No snapshots advanced, so the pages are re-assessed next run and the rows "
                   f"already created are not duplicated. Error: {esc(str(st['create_error'])[:200])}")
    elif mode != "baseline" and outcome and outcome != "success":
        alarm = True
        out.append(f"⚠️ *The assessment step did not finish ({esc(outcome)}).* No proposals were created and "
                   f"no snapshots advanced, so every change is retried next run. See the scan log.")
    if st.get("failed"):
        alarm = True
        out.append(f"⚠️ *Claude failed on {st['failed']} of {st.get('candidates', '?')} pages.* Those pages are "
                   f"held and retried next run. Last error: {esc(str(st.get('last_error', ''))[:200])}")
    if st and not st.get("options_loaded"):
        alarm = True
        out.append("⚠️ *No Master Variables options were loaded*, so every linked proposal is marked as a "
                   "new option. Check the Master Variables 'Variable' field.")
    if st.get("dry_run"):
        out.append(f"Dry run: {st.get('candidates', 0)} changed page(s) would be assessed. "
                   f"Nothing was sent to Claude and nothing was committed.")
    elif st.get("shadow") and mode != "baseline":
        out.append("⚠️ *Shadow mode is on* (repo variable PROPOSALS_SHADOW is not \"false\"): nothing was "
                   "written to Airtable and the assessed pages are held, so they are assessed again once it is off.")
    held = (st.get("deferred") or 0) + (st.get("held_by_caps") or 0)
    if held:
        out.append(f"{held} page(s) over the per-run caps are held for the next run.")
    if st and not st.get("dry_run") and not st.get("candidates") and outcome in ("", "success"):
        out.append("Claude was not called: no changed page passed the filters this run.")
    if st.get("assessed"):
        cost = st.get("est_cost_usd")
        extra = []
        if st.get("refused"):
            extra.append(f"{st['refused']} declined")
        if st.get("unverified"):
            extra.append(f"{st['unverified']} dropped because the quote was not on the page")
        if st.get("thin_evidence"):
            extra.append(f"{st['thin_evidence']} dropped for a too-short quote")
        if st.get("shortened"):
            extra.append(f"{st['shortened']} long diff(s) shortened")
        out.append(f"Claude ({esc(st.get('model', ''))}, effort {esc(st.get('effort', ''))}): "
                   f"{st['assessed']} page(s) assessed"
                   + (f", est. ${cost:.2f}" if isinstance(cost, (int, float)) else "")
                   + (f" ({'; '.join(extra)})" if extra else "") + ".")
    return out, alarm


def proposal_lines(props: list) -> list[str]:
    """Proposal queue grouped by vendor (brief §6). Lifecycle proposals get a
    distinct marker so they are never skimmed past."""
    by_v = defaultdict(list)
    for p in props:
        by_v[p.get("vendor", "?")].append(p)
    lines = []
    for vendor in sorted(by_v):
        items = by_v[vendor]
        lines.append(f"\n*{esc(vendor)}* ({len(items)} proposal(s))")
        for p in items:
            mark = ""
            if p.get("field") == "Status (lifecycle)":
                mark = "  ⚠️ LIFECYCLE"
            elif p.get("proposal_type") == "new_canonical_option":
                mark = "  ✎ NEW OPTION (add it to Master Variables first)"
            if p.get("linked"):  # tag lists: apply adds the option, it never removes one
                now = str(p.get("current_value") or "∅")
                lines.append(f"  {esc(p.get('field'))}: add {esc(p.get('proposed_value'))}{mark}  "
                             f"(now: {esc(now[:80] + ('…' if len(now) > 80 else ''))})")
            else:
                lines.append(f"  {esc(p.get('field'))}: {esc(p.get('current_value') or '∅')} → "
                             f"{esc(p.get('proposed_value'))}{mark}")
            lines.append(f"    \"{esc((p.get('evidence_quote') or '')[:160])}\"")
            lines.append(f"    {esc(p.get('source_url', ''))}  ·  {esc(p.get('change_type', ''))}  ·  "
                         f"conf {p.get('confidence')}")
    low_conf = sum(1 for p in props if (p.get("confidence") or 0) < 0.7)
    lines.append(f"\n{len(props)} pending, {low_conf} below 0.7 confidence.")
    return lines


def bucket_lines(s: dict, sevs: tuple, limit: int = 15) -> list[str]:
    out = []
    for sev in sevs:
        items = s.get("buckets", {}).get(sev, [])
        if not items:
            continue
        out.append(f"\n*{LABEL[sev]}* ({len(items)})")
        for b in items[:limit]:
            extra = f" [{esc(b['type'])}]" if b.get("type") else ""
            st = f" ({b['status']})" if isinstance(b.get("status"), int) and b["status"] >= 300 else ""
            out.append(f"• {esc(b.get('vendor', '?'))}: {esc(b.get('url', ''))}{extra}{st}")
        if len(items) > limit:
            out.append(f"  …and {len(items) - limit} more")
    return out


def fit(head: list[str], body: list[str], tail: list[str]) -> str:
    """The head always survives; body, then tail, are added line by line until
    MAX_CHARS, and a pointer to the review link replaces whatever does not fit."""
    out = list(head)
    size = sum(len(ln) + 1 for ln in out)
    for section in (body, tail):
        for i, ln in enumerate(section):
            if size + len(ln) + 1 > MAX_CHARS:
                out.append(f"\n…{len(section) - i} more line(s) not shown. The full list is behind the review link above.")
                return "\n".join(out)
            out.append(ln)
            size += len(ln) + 1
    return "\n".join(out)


def build(summary: dict, proposals: list, st: dict, outcome: str = "", mode: str = "monitor") -> str:
    status, alarm = status_lines(st, outcome, mode)
    shadow = bool(st.get("shadow"))
    # Ping Ben when there is something to act on: new proposals or a failure.
    # A quiet month and a shadow preview post without the mention.
    ping = (bool(proposals) and not shadow) or alarm
    if alarm and outcome not in ("", "success"):
        title = f"*Vendor scan {esc(summary.get('date') or st.get('date', ''))}: needs attention*"
    elif proposals and shadow:
        title = f"*PREVIEW (shadow mode, nothing written to Airtable): {len(proposals)} proposal(s)*"
    elif proposals:
        title = f"*Vendor proposals: {len(proposals)} pending*"
    else:
        title = f"*Vendor scan {esc(summary.get('date') or st.get('date', ''))}*"
    head = [(BEN + " " if ping else "") + title]
    if summary:
        c = summary.get("counts", {})
        head.append(f"Scanned {summary.get('scanned', 0)} URLs. Since the last fetch: "
                    f"{len(summary.get('changed_vendors', []))} vendor(s) changed, {c.get('error', 0)} error(s), "
                    f"{c.get('noisy', 0)} noisy.")
    if st and not st.get("dry_run"):
        pending = (st.get("changed_files") or 0) + (st.get("first_sightings") or 0)
        head.append(f"Since the last review: {pending} page(s) changed, {st.get('candidates', 0)} worth assessing.")
    head += status
    if not shadow:
        head.append(f"Review + approve: {QUEUE_URL}")
    if REVIEW_URL:
        head.append(f"Scan log: {REVIEW_URL}")
    if proposals:
        body, tail = proposal_lines(proposals), bucket_lines(summary, ("error", "noisy"), 10)
    else:
        body, tail = bucket_lines(summary, ("high", "normal", "low", "error", "noisy")), []
    return fit(head, body, tail)


def main() -> None:
    summary = load(SUMMARY_IN, {})
    proposals = load(PROPOSALS_IN, []) or []
    st = load(STATUS_IN, {})
    outcome = os.environ.get("ASSESS_OUTCOME", "")
    mode = os.environ.get("RUN_MODE") or "monitor"
    if not (summary or proposals or st or outcome):
        print("Nothing to post.")
        return
    text = build(summary, proposals, st, outcome, mode)
    if not WEBHOOK:
        print("SLACK_WEBHOOK_URL unset; would have posted:\n")
        print(text)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print("ERROR: the SLACK_WEBHOOK_URL secret is missing, so nobody was notified.", file=sys.stderr)
            sys.exit(1)
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
