# Speero Testing Tools — Data

Do not edit by hand. `testing-tools.json` is generated from Airtable by the
sync workflow in `.github/workflows/sync.yml`, which runs every 6 hours and on
manual dispatch.

Served via jsDelivr as the embed's data source:

```
https://cdn.jsdelivr.net/gh/speerotools/testing-tools-data@main/testing-tools.json
```

## Setup

The workflow needs a repository secret `AIRTABLE_TOKEN` — an Airtable Personal
Access Token with `data.records:read` and `schema.bases:read` scopes, granted
access to the Testing Tools base.

To run manually: Actions tab → Sync Airtable to JSON → Run workflow.
