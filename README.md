# Speero Testing Tools Data

Airtable is the source of truth. GitHub Actions publish it to the live site and
keep the vendor records fresh. Nothing here is edited by hand.

Published data (served via jsDelivr, read by the site embed):

```
https://cdn.jsdelivr.net/gh/speerotools/testing-tools-data@main/testing-tools.json
```

## Two loops

**1. Publish (automatic).** Every 6 hours `sync.yml` turns Airtable into the
published JSON, purges the CDN, and updates the Webflow vendor pages. Edit
Airtable and the site follows on its own. The sync refuses to publish if a
record fails to transform, if the vendor list comes back empty, or if it drops
by more than 3 vendors or 10% (whichever is larger) at once, so a bad Airtable read can never wipe
pages. For an intended large removal, set the repo variable `ALLOW_SHRINK=true`
for one run. The Webflow step deletes at most `MAX_DELETE` (default 3) items per run.

**2. Freshness (automatic scan, human approves).** On the 1st of each month
`enrich.yml` visits every vendor URL and snapshots it, Claude (Opus 5.5) turns
the changes into exact record edits (each with an evidence quote, checked
against the page text and dropped when it isn't there), and they land in the Airtable **Proposals** table with a
Slack digest that tags the reviewer. Approved rows are written back by
`apply.yml` every morning. **Nothing changes a vendor record without approval.**

## How the scan decides what to look at

git is the review ledger. The last commit holds the last reviewed snapshot of
every page, so:

- A page whose snapshot differs from the last commit is an unreviewed change.
- A page with no committed snapshot yet is a first sighting.
- A snapshot is only committed after Claude assessed it. If the assessment
  fails, the run is a dry run, or a page is held back by a cap, its snapshot is
  not committed and the change is picked up again on the next run. Nothing is
  silently dropped.

Caps hold overflow for the next run instead of discarding it: `MAX_ASSESS`
pages per run (default 300), 4 proposals per page, 6 per vendor, 150 per run.
Proposals that match an existing Pending, Approved, Applied or Rejected row are
skipped; Failed and Stale rows do not block a fresh proposal.

## What's automatic vs manual, each month

- **Automatic:** the scan, the proposals in Airtable, the Slack digest, and applying approved rows (daily at 05:30 UTC, before the 06:00 sync publishes them).
- **Manual (a few minutes):** set good proposals to **Approved** in the Airtable Proposals view. If a proposal needs a new option, add it to Master Variables first. Tag-list proposals ("Integrations: add X") only add; if the rationale says an existing tag is wrong, remove it by hand. If nothing real changed, there's nothing to do.

## Workflows

| Workflow | When | What |
|---|---|---|
| `sync.yml` | every 6h + dispatch | Airtable to JSON, CDN purge, Webflow pages |
| `enrich.yml` | monthly + dispatch | scan pages, Claude drafts proposals, Slack digest, commit reviewed snapshots |
| `apply.yml` | daily + dispatch (dispatch defaults to dry run) | write Approved proposals to Airtable |

`enrich.yml` and `apply.yml` share one concurrency group, so they never write
Airtable at the same time. Manual `enrich.yml` inputs:

- `limit`: only process the first N URLs. Use a small number (for example 10) to check a change end to end.
- `dry_run`: list what Claude would assess, call nothing, commit nothing.
- `max_assess`: override `MAX_ASSESS` for this run.
- `mode = baseline`: fetch and commit snapshots without assessing (fresh start).

## Scripts (`.github/scripts/`)

- `sync.py`: build the published JSON from Airtable (with the shrink guard above).
- `webflow_sync.py`: reconcile the Webflow CMS items, names and per-vendor SEO fields.
- `enrich.py`: fetch, hash and snapshot every URL (`snapshots/`); triage change.
- `assess.py`: unreviewed snapshot changes to Claude to proposed field changes (field whitelist, evidence check, caps, cost estimate).
- `apply.py`: write Approved proposals (whitelist re-check, staleness check, sets Last Vendor Scrape so the rescore formula trips).
- `notify_slack.py`: the Slack digest (scan status, cost, proposal queue link).
- `create_proposals_table.py`: one-time setup of the typed Proposals table.

## Secrets / variables

- Secrets: `AIRTABLE_TOKEN` (read + write + schema.bases:read), `WEBFLOW_TOKEN`, `SLACK_WEBHOOK_URL`, `ANTHROPIC_API_KEY`.
- Variables:
  - `PROPOSALS_SHADOW`: `false` sends proposals to Airtable; `true` (default) is preview only.
  - `ANTHROPIC_MODEL` (default `claude-opus-5-5`), `ANTHROPIC_EFFORT` (default `medium`), `MAX_ASSESS` (default 300).
  - `SLACK_MENTION`: who the digest and apply summary tag, as a Slack member id (for example `U0GP0BBEW`). Optional: unset means Ben Labay (`U0GP0BBEW`).
  - `WEBFLOW_PUBLISH`, `ALLOW_SHRINK`.
