# Speero Testing Tools — Data

Airtable is the source of truth. GitHub Actions turn it into the published data
and keep the vendor records fresh. Nothing here is edited by hand.

Published data (served via jsDelivr, consumed by the embed):

```
https://cdn.jsdelivr.net/gh/speerotools/testing-tools-data@main/testing-tools.json
```

## Workflows

| Workflow | When | What it does |
|---|---|---|
| `sync.yml` | every 6h + dispatch | Airtable → `testing-tools.json`; reconciles the Webflow CMS (per-vendor pages) and purges the CDN. |
| `enrich.yml` | monthly (1st) + dispatch | Fetches every active vendor URL, hashes + snapshots it, triages what changed, and (in shadow) proposes Airtable edits, then posts a Slack digest. |
| `apply.yml` | dispatch only | Writes human-approved proposals back to Airtable. Dry-run by default. |

## Scripts (`.github/scripts/`)

- `sync.py` — build the published JSON from Airtable.
- `webflow_sync.py` — reconcile the Webflow CMS collection to the data.
- `enrich.py` — fetch + hash + snapshot each URL; write change fields back; emit the scan summary and page snapshots (`snapshots/`).
- `assess.py` — turn a snapshot diff into proposed field changes via Gemini (structured output, strict field whitelist + evidence). Shadow by default.
- `apply.py` — write approved proposals; whitelist re-check, staleness check, and a `Last Vendor Scrape` bump to trigger a rescore.
- `notify_slack.py` — the Slack digest (scan summary + proposal queue).

## The monthly loop

```
enrich (fetch, hash, snapshot, triage)
   → assess (diff → Gemini → proposals, shadow)
   → Slack digest (mentions the reviewer)
   → human approves in the Airtable Pending Review view
   → apply (writes approved changes, dispatch)
```

Nothing writes vendor facts without a human approving first.

## Secrets / variables

- Secret `AIRTABLE_TOKEN` — PAT with `data.records:read` + `data.records:write` + `schema.bases:read`.
- Secret `WEBFLOW_TOKEN`, `SLACK_WEBHOOK_URL`, `GEMINI_API_KEY`.
- Variables: `WEBFLOW_PUBLISH`, `PROPOSALS_SHADOW`, `PROPOSALS_QUEUE_URL`.

Proposal Queue setup and phasing: see `phase-e/proposals-table-setup.md`.
