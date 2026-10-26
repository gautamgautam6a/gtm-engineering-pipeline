# GTM Engineering: billing data to HubSpot

This learning project turns synthetic Stripe billing and product-usage data into one billing summary per customer. It detects changes and prepares HubSpot Billing Account updates. Preview is the default: it sends nothing and does not save successful-sync state.

The project follows the [Ducktate GTM Engineering lesson](https://ducktate.com/learn/gtm-engineering/data-architecture-reverse-etl/). It is a local demonstration with a Developer test account integration, not a production deployment.

## Implementation and evidence

| Status | What it means |
| --- | --- |
| Implemented locally | Billing model, quality checks, change detection, atomic JSON state, payload validation, row-count guard, webhook inbox, and identity SQL. |
| Tested with mocks | HubSpot account verification, upserts, HTTP failures, retries, and successful-state handling use mocked responses in automated tests. |
| Live-verified by the project owner | Account verification passed for portal `247794315`, type `DEVELOPER_TEST`. One synthetic `cus_mock005` upsert was confirmed, inspected in HubSpot, and manually deleted. That test did not save sync state. |
| Latest recorded suite | 93 passed, 0 failed before this documentation update; not a new run or clean-install result. |
| Incomplete | Live Stripe ingestion, CRM contact ingestion, webhook-to-billing processing, contact associations, and complete live end-to-end verification. |

**Fresh installation is unverified.** The dependency pins match the existing environment but have not been tested in a separate clean environment. Direct dependency pins do not lock all transitive packages.

## Architecture

```text
Mock Stripe customers/subscriptions + product usage
    → raw DuckDB tables
    → billing SQL + transactional quality checks
    → account_billing
    → compare with last successful-sync JSON state
    → validate/map HubSpot properties
    → preview OR explicitly enabled sender
    → save each record's state only after confirmed success
```

Separately, the webhook receiver verifies signed events and stores them in a persistent SQLite inbox. No worker applies them to billing tables yet. Identity SQL matches sample contact domains to billing accounts; contacts currently come only from test fixtures.

## Important files

| File | Purpose |
| --- | --- |
| `seed_mock.py` | Generates six synthetic customers, subscriptions and usage events in the current folder's `gtm.duckdb`. |
| `Models/account_billing.sql` | Produces one billing summary per customer. |
| `model_checks.py` | Defines customer-key and MRR quality checks. |
| `run_model.py` | Materializes the model and rolls back the transaction if checks fail. |
| `reverse_etl.py` | Reads source data, compares state, previews changes and coordinates explicitly enabled writes. |
| `hubspot_payloads.py` | Validates source values and maps destination properties. |
| `hubspot_sender.py` | Verifies the account and sends unique-key upserts. |
| `source_safety.py` | Rejects suspicious source row-count drops. |
| `webhook_app.py` | Verifies signed Stripe events and stores them idempotently. |
| `Models/contact_account_matches.sql` | Matches contacts to account candidates using exact email domains. |
| `test_*.py`, except `test_join.py` | Supported isolated automated test modules. |
| `test_join.py` | Optional exploratory queries that open the project database; not an isolated test or the authoritative billing calculation. |

## Setup

Use Windows PowerShell from your project copy. The observed environment uses Python **3.14.8**; other versions are unverified. Offline tests and demos need no credentials.

```powershell
Set-Location C:\Users\gauta\gtm-stage-1
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Adjust the project path for your machine. Requirements contain only DuckDB, Flask, Requests and Stripe. `unittest` and SQLite support are included with Python.

The optional `test_join.py` uses DataFrame output requiring pandas separately (observed version: `3.1.0`). It is excluded from the supported setup and suite. Avoid unrestricted test discovery because importing it opens `gtm.duckdb`.

## Automated tests

```powershell
.\.venv\Scripts\python.exe -B -m unittest -v test_account_billing test_billing_integration test_hubspot_payloads test_hubspot_sender test_identity_resolution test_source_safety test_webhook
```

Tests use in-memory or disposable databases and state, including temporary folders beside project files. Sender HTTP is mocked. Coverage includes billing edge cases, model rollback, NEW/CHANGED/UNCHANGED, 4,900 cents changing to zero, failed-write retry eligibility, account mismatches, timestamps, row-count boundaries, webhook signatures and persistent duplicates, and free-email exclusions.

## Safe disposable demo

**Do not run `seed_mock.py` from the project directory.** It can replace raw tables in the existing local `gtm.duckdb`. Use the new temporary folder below. Do not add `--write` to these commands.

Run from the project folder after setup:

```powershell
$projectPath = (Get-Location).Path
$pythonPath = Join-Path $projectPath ".venv\Scripts\python.exe"
$demoPath = Join-Path ([System.IO.Path]::GetTempPath()) ("gtm-demo-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $demoPath | Out-Null

Push-Location $demoPath
try {
    & $pythonPath -B (Join-Path $projectPath "seed_mock.py")
    if ($LASTEXITCODE -ne 0) { throw "Demo seeding failed." }
} finally {
    Pop-Location
}

& $pythonPath -B .\run_model.py --database "$demoPath\gtm.duckdb" --apply
if ($LASTEXITCODE -ne 0) { throw "Demo model build failed." }

& $pythonPath -B .\reverse_etl.py --database "$demoPath\gtm.duckdb" --state "$demoPath\sync_state.json"
```

Seeding writes only into the new demo folder. `--apply` builds and checks that disposable billing table. The last command previews it read-only; missing state means six NEW customers, with no state file created. Usage values depend on the seed time, so results are not a fixed historical snapshot.

For full payload output, reuse the same variables. This preserves any prior `DRY_RUN` setting:

```powershell
$previousDryRun = $env:DRY_RUN
try {
    $env:DRY_RUN = "1"
    & $pythonPath -B .\reverse_etl.py --database "$demoPath\gtm.duckdb" --state "$demoPath\sync_state.json"
} finally {
    if ($null -eq $previousDryRun) {
        Remove-Item Env:DRY_RUN -ErrorAction SilentlyContinue
    } else {
        $env:DRY_RUN = $previousDryRun
    }
}
```

The temporary folder remains for inspection. Remove it manually later only after checking its path. These instructions contain no live-sync or deletion command.

## Billing rules and output

MRR means monthly recurring revenue. The model multiplies item prices by quantity, normalizes monthly and annual intervals including interval counts, aggregates items and subscriptions, and rounds once to integer cents. Active and past-due subscriptions count; other statuses contribute zero. Week/day prices are unsupported. Customers without subscriptions are preserved.

| Field | Meaning |
| --- | --- |
| `stripe_customer_id` | Stable source customer ID and HubSpot unique upsert key. |
| `customer_name` | Required nonblank string; valid whitespace is preserved. |
| `company_domain` | Customer email domain, excluding configured free-email domains; nullable. |
| `mrr_cents` | Nonnegative integer cents, not floating-point dollars. |
| `subscription_status` | Active takes precedence, then past_due; otherwise the maximum remaining status text is selected. |
| `active_users_30d` | Distinct usage emails within 30 days for the matching domain. |
| `last_event_at` | Most recent usage event as UTC ISO 8601 text; nullable. |

Quality checks require present, unique customer keys, nonnegative/non-null MRR, and positive MRR for active customers. Payload validation checks names, IDs, integer values and valid UTC timestamps ending in `Z` or `+00:00`, with optional fractions. Valid timestamp strings are preserved. Payload properties become strings; nulls are omitted. Omission does not explicitly clear an existing remote value.

## Modes, change detection and state

- Default preview reads the source and state, prints classifications, and sends/saves nothing.
- `DRY_RUN=1` prints full pending payloads without sending or saving.
- `--simulate` uses made-up records and temporary state, without reading the billing database.
- `--write` explicitly enables all pending source records. It rejects combination with `DRY_RUN=1` or `--simulate`. There is no one-record CLI filter.

Missing state means NEW. An existing ID with identical mapped values is UNCHANGED; a mapped value difference is CHANGED. Malformed or legacy state stops processing. A confirmed individual write saves the full mapped values using a sibling temporary file and atomic replacement. Failed or uncertain writes do not advance that record's state.

Use one state writer at a time; there is no concurrency lock. A remote success followed by local save failure needs investigation before retrying. State is local memory, not proof of remote existence; remote deletion does not invalidate local state automatically.

## Safety controls

The sender verifies portal `247794315` and account type `DEVELOPER_TEST` via the read-only account-information endpoint before its first POST each session. It targets object `2-270744393` using `stripe_customer_id` for upserts. Failed verification blocks writes.

The row-count guard checks the entire source against saved customer count: six saved/two current stops; six saved/three current does not trigger this guard. Empty state permits a first sync. This is not deletion or mirror behavior.

Timeouts, connection failures and ambiguous success responses stop without immediate retry. HTTP 429 and 5xx can retry up to three attempts with bounded waits. A 5xx may leave the write outcome uncertain. Inspect HubSpot before a manual retry. Controlled one-record tests can set `sender.max_attempts = 1` without changing application defaults.

## Webhook inbox

Flask exposes POST `/stripe/webhook`. Configuration uses `STRIPE_WEBHOOK_SECRET` and optional `INBOX_DB` (a separate `.sqlite` file; default `inbox.sqlite`). Creating the app creates the inbox, so it is not a read-only operation.

The official Stripe library verifies raw request bytes before event fields are used or stored. The five-minute stale rule checks the signature timestamp, not event `created`. A persistent event-ID primary key prevents duplicates from replacing payloads or resetting processed status. Failed storage is not acknowledged as success. Tests need neither live Stripe nor a public tunnel.

No worker consumes the inbox, fetches current Stripe objects, or updates billing data yet.

## Identity resolution and proposed ownership

Identity SQL lowercases domains and matches them exactly, excluding the listed free-email providers. Unmatched contacts remain. Shared domains can return multiple candidates; the SQL does not guess a winner or collapse subdomains. `raw_crm_contacts` is currently a test fixture, not an ingested source.

This table proposes responsibilities; there is no general field-ownership enforcement framework:

| Fields | Intended owner | Why |
| --- | --- | --- |
| Customer ID/name, billing status, MRR | Stripe source / warehouse derivation | Billing facts originate upstream. |
| Company domain | Warehouse derivation | Derived from source email with exclusions. |
| Active users and last event | Usage source / warehouse derivation | Calculated from product events. |
| Sales notes, deal stages, manual CRM fields | CRM | Sales activity stays in the sales system; these fields are outside the payload. |

## Optional HubSpot configuration

Offline setup needs no HubSpot credential. Live sending reads `HUBSPOT_API_KEY` from the current process environment as a Bearer token. Never print it, hardcode it, commit it, or include it in screenshots. These scripts do not automatically load `.env`; a value in that file alone does not configure them.

The seven payload fields were previously compared with the Developer test object. Actual HubSpot requires `customer_name` and has a unique `stripe_customer_id`. The local lesson schema constant differs in labels, required properties and searchable properties; it is not an executed schema migration.

Live operations need separate review and explicit authorization. Review the exact one-record payload, verify the account, confirm the synthetic record in the test portal and manually delete that exact record if cleanup is intended. The offline quickstart is not authorization to sync all six records.

## Troubleshooting, limitations and publication

- Missing credential: offline tests still work; separately approved live operations need the environment set in the same terminal.
- Account mismatch or account-check error: stop instead of weakening the guard.
- Malformed state: inspect and recover deliberately; do not delete it just to force resending.
- Row-count failure: investigate source completeness before sending.
- Uncertain write: inspect HubSpot before retrying; no local state does not prove remote absence.

Before publishing, review credentials, databases, state, webhook inboxes, caches, exports and screenshots. `.gitignore` does not untrack existing files or erase Git history. Several mock company domains are not reserved test domains and must not be treated as safe contact destinations.

Remaining work includes contact ingestion, a webhook worker, fresh-environment verification, concurrent-writer protection and a complete controlled end-to-end test. No automated CRM deletion/mirror workflow or scheduled production operation is implemented.
