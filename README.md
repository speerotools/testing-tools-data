# Speero Testing Tools — Data

Airtable is the source of truth. GitHub Actions publish it to the live site and
keep the vendor records fresh. Nothing here is edited by hand.

Published data (served via jsDelivr, read by the site embed):

```
https://cdn.jsdelivr.net/gh/speerotools/testing-tools-data@main/testing-tools.json
```

## Two loops

**1. Publish (automatic).** Every 6 hours `sync.yml` turns Airtable into the
published JSON, updates the Webflow vendor pages, and refreshes the CDN. Edit
Airtable, the site follows on its own.

**2. Freshness (automatic scan, human approves).** On the 1st of each month
`enrich.yml` visits every vendor URL, detects what changed, has Gemini draft the
exact record edits (each with an evidence quote), and posts them to Slack + the
Airtable **Proposals** table. A person approves the good ones, then `apply.yml`
writes them back. **Nothing changes a vendor record without approval.**

## What's automatic vs manual, each month

- **Automatic:** the scan runs itself, drafts proposals, writes them to Airtable, posts the Slack digest (tags the reviewer).
- **Manual (a few minutes):** approve proposals in the Airtable **Pending Review** view, then run **apply-proposals**. If nothing real changed, there's nothing to do.

## Workflows

| Workflow | When | What |
|---|---|---|
| `sync.yml` | every 6h + dispatch | Airtable → JSON, Webflow pages, CDN purge |
| `enrich.yml` | monthly + dispatch | scan pages, detect change, draft proposals, Slack digest |
| `apply.yml` | dispatch only (dry-run default) | write approved proposals to Airtable |

## Scripts (`.github/scripts/`)

- `sync.py` — build the published JSON from Airtable.
- `webflow_sync.py` — reconcile the Webflow CMS pages + per-vendor SEO fields.
- `enrich.py` — fetch, hash, and snapshot every URL (`snapshots/`); triage change.
- `assess.py` — snapshot diff → Gemini → proposed field changes (strict whitelist + evidence + cost guards).
- `apply.py` — write approved proposals (whitelist re-check, staleness check, rescore trigger).
- `notify_slack.py` — the Slack digest (scan summary + proposal queue).
- `create_proposals_table.py` — one-time: build the typed Proposals table.

## Secrets / variables

- Secrets: `AIRTABLE_TOKEN` (read + write + schema.bases:read), `WEBFLOW_TOKEN`, `SLACK_WEBHOOK_URL`, `GEMINI_API_KEY`.
- Variables: `WEBFLOW_PUBLISH`, `PROPOSALS_SHADOW` (false = proposals go to Airtable; true = preview only), `PROPOSALS_QUEUE_URL`.

Proposal Queue setup + phasing: `phase-e/proposals-table-setup.md`.
