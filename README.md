# financial-api

A small, authenticated, **stateless Plaid API** for a single household. Shopped
owns transaction storage, categorization, budget logic, and sync checkpoints.
This service only retrieves financial data from already-connected Plaid items.

- Python 3.12 / FastAPI; direct Plaid HTTPS requests with bounded timeouts.
- No database, mounted volume, scheduler, browser UI, or server-side cursor state.
- Bearer authentication on every data route. `/health` is the only public route.
- No linking, token exchange, payment actions, categorization, or budget endpoints.
- One configured item per request: failures never silently produce partial multi-bank results.

## Configuration

Inject these as runtime environment variables (Dokploy's Environment tab).
The service does **not** implicitly read `.env` files. Never commit credentials,
exported item JSON/base64, SQLite databases, or financial fixtures to this public repo.

| Variable | Required | Purpose |
| --- | --- | --- |
| `FINANCIAL_API_TOKEN` | Yes | Random bearer secret, at least 32 characters; generate with `openssl rand -hex 32` |
| `FINANCIAL_PLAID_CLIENT_ID` | Yes | Existing Plaid client ID |
| `FINANCIAL_PLAID_SECRET` | Yes | Plaid secret matching the selected environment |
| `FINANCIAL_PLAID_ENVIRONMENT` | No | `sandbox` (default) or `production`; any other value fails startup |
| `FINANCIAL_PLAID_ITEMS_B64` | Recommended | Standard base64 of the UTF-8 JSON array; one line, Dokploy-safe |
| `FINANCIAL_PLAID_ITEMS_JSON` | Fallback | Raw JSON array, used only when B64 is empty/unset |
| `FINANCIAL_ENCRYPTION_KEY` | For encrypted tokens | Existing Spendy Fernet encryption key |

Every setting also accepts its `SPENDY_` equivalent, for example
`SPENDY_PLAID_SECRET`. `FINANCIAL_` wins when both exist, even when empty.
Spendy's `development`→sandbox alias is intentionally not accepted: select the
actual environment explicitly. A weak old `SPENDY_API_TOKEN` must be replaced.
Invalid or empty connection configuration fails startup rather than appearing healthy.

Supply at least one connection variable. Nonempty `FINANCIAL_PLAID_ITEMS_B64`
takes priority over JSON; invalid base64 fails startup without falling back.
Use standard padded base64, without internal line breaks or shell quotes.
Surrounding whitespace is trimmed. Base64 is transport encoding, **not encryption**;
keep the value secret and retain the existing Fernet key for encrypted tokens.

The decoded JSON (or raw `FINANCIAL_PLAID_ITEMS_JSON`) uses this schema, with
unique, real Plaid item IDs:

```json
[
  {
    "item_id": "example-item-id",
    "institution_name": "Example Bank",
    "access_token_encrypted": "replace-with-existing-fernet-ciphertext"
  }
]
```

Or use `access_token` instead of `access_token_encrypted`. Never specify both.
Plaintext tokens are supported for secret-manager deployments; encrypted tokens
let you reuse Spendy's stored connections without decrypting them during export.
The API decrypts them once on startup and keeps them only in process memory.
The API never returns access tokens, client secrets, or encryption keys.
Changing the configured connections requires a restart/redeploy. There is no
multi-user authorization: possession of the bearer token grants access to all
configured items. Keep it server-side in Shopped, never in browser code.

## Migrate existing Spendy connections

**Environment variables alone are insufficient.** Spendy stores long-lived
Plaid access tokens in `plaid_items` in its SQLite database. You need those rows
as well as the existing encryption key and matching Plaid environment/credentials.
You do not need to reconnect banks if those tokens are still valid.

On a trusted machine/container with access to the existing database:

```bash
python scripts/export_spendy_items.py \
  --db /path/to/spendy.sqlite \
  --out /secure/path/plaid-items.b64 --format base64
```

The helper uses SQLite read-only mode, exports **only encrypted connections**,
creates a new owner-only (`0600`) file, refuses overwrite, and does not print
secrets. Copy the file's single base64 line into Dokploy's `FINANCIAL_PLAID_ITEMS_B64` value
and copy the existing `SPENDY_ENCRYPTION_KEY` (or rename it to
`FINANCIAL_ENCRYPTION_KEY`). Delete the temporary export after secure transfer.
The default export format remains JSON for compatibility (`--format json`).
Unset `FINANCIAL_PLAID_ITEMS_JSON` when using base64 to avoid stale configuration.

Use a proper SQLite backup if moving the DB; do not copy a live WAL database's
main file without its committed WAL contents. Include errored items too so
reauthentication needs remain visible, rather than silently dropping banks.

This exports **no transactions and no cursor**. For a fresh Shopped import, start
sync without a cursor. Reusing Spendy's advanced cursor without its corresponding
transaction history would skip historical records. Plaid only returns history
available for the item; older Spendy history and custom categories would require
a separate data migration. No household data has been included in this repo.

## API

For all data routes:

```http
Authorization: Bearer <FINANCIAL_API_TOKEN>
```

All responses have `Cache-Control: no-store`. Invalid authentication returns 401,
unknown configured item IDs return 404, and invalid parameters return 422.
Validation errors do not echo request input. OpenAPI is available at
`GET /openapi.json` with bearer authentication; interactive docs are disabled.

### `GET /health`

Unauthenticated liveness only: `{"status":"ok"}`. This does **not** assert bank
connectivity or data freshness. Startup configuration must validate first.

### `GET /items`

Lists configured `item_id` and optional `institution_name` only. Start here and
iterate over the items when retrieving all connected accounts/transactions.
Institution names are configuration labels, not a live institution lookup.

### `GET /accounts?item_id=...`

Retrieves the selected item's accounts from Plaid `/accounts/get`, returning
account IDs, names, types, masks, and balances. A missing balance is `null`, not
zero. Balances reflect Plaid's cached data; this is not a paid real-time balance
refresh. One failing item does not prevent the caller from fetching other items.

### `GET /transactions?item_id=...&start_date=2026-01-01&end_date=2026-01-31`

A date-range read from Plaid `/transactions/get`. Dates are required, inclusive,
and use the provider's transaction dates (no timezone conversion).

- `count`: 1–500; default 500.
- `offset`: nonnegative integer; default 0.
- Returns `item_id`, `transactions`, `accounts`, `count`, `total_transactions`,
  and `next_offset` (`null` on the last page).
- Repeat with `next_offset` to read the next page, keeping dates/item unchanged.
- This is an ad-hoc view, **not a stable snapshot**: changes during pagination
  can shift offsets. Use sync for reliable incremental ingestion and deletions.

### `POST /transactions/sync`

One page of Plaid `/transactions/sync`; no hidden looping or stored cursors.

```json
{"item_id":"example-item-id","cursor":null,"count":500}
```

Returns `item_id`, `added`, `modified`, `removed`, `accounts`, `next_cursor`,
`has_more`, and `transactions_update_status`.
`removed` entries contain `item_id`, `transaction_id`, and optional `account_id`.
The initial cursor may be omitted or `null`. All cursor strings are opaque;
do not decode, modify, or put them into URLs/logs.

**Shopped ingestion contract:**

1. Keep a durable committed cursor **per item and Plaid environment** in Shopped.
   On first import, omit it. Never share checkpoints between items/environments.
2. Remember that starting cursor. Fetch a page; while `has_more` is true, fetch
   the next page using `next_cursor`. Stage pages without publishing them as a
   completed batch. Allow only one ingestion run at a time for each item.
3. Apply changes in page order: upsert `added`/`modified` by transaction ID and
   delete `removed` IDs. Commit those changes and the final `next_cursor`
   together, **only after the entire pagination batch succeeds**. Preserve
   Shopped's user categories separately from provider categories.
4. If a request fails, retain the prior committed cursor. Retry with backoff.
   If the API returns 409 / `TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION`, discard
   the staged batch and restart at the cursor from step 1, **not the last page's
   cursor**. A crashed import must be safe to replay without duplicating rows.
5. Initial synchronization can return empty arrays and an empty cursor while
   Plaid prepares history. Inspect `transactions_update_status` and poll later;
   an empty response is not proof that the connected accounts have no spending.

Sync does not force Plaid to fetch new bank data. There is no webhook receiver or
`/transactions/refresh` endpoint in this service; polling reads Plaid's latest
available data. No new paid refresh calls are introduced.

### Data semantics and errors

- Plaid signs are preserved: positive is money out; negative is money in.
  Transfers and card payments are **not** automatically excluded or categorized.
- Preserve `pending_transaction_id` to reconcile pending→posted replacements.
- `personal_finance_category` is preserved along with the legacy category list.
- Missing ISO currency remains `null`; unofficial currency is separate. No GBP
  or USD fallback, and no conversion. Amounts retain Plaid's numeric values;
  use decimal/money types when persisting and calculating in Shopped.
- `ITEM_LOGIN_REQUIRED` and consent errors require fixing the connection through
  the existing linking flow. This API does not manage connection lifecycle.
- Plaid/network failures return sanitized codes, not upstream response bodies.
  HTTP 429 means rate limited; 504 means timeout; 502 is an upstream/provider
  failure (including a not-yet-ready product); 409 requests a sync restart.
  Retry transient failures with bounded exponential backoff. Don't retry login
  or permission failures indefinitely. Unexpected internal failures return 500.

## Run and test locally

Install [uv](https://docs.astral.sh/uv/), then:

```bash
uv sync --frozen
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
# Supply real configuration via exported environment variables, then:
uv run uvicorn financial_api.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

Tests use synthetic data and an in-process mock Plaid transport. They require
neither real credentials nor network calls to Plaid. `uv.lock` pins dependencies.

Example server-to-server reads (variables already supplied through your secrets system):

```bash
curl --fail-with-body "$FINANCIAL_API_URL/items" \
  -H "Authorization: Bearer $FINANCIAL_API_TOKEN"

curl --fail-with-body "$FINANCIAL_API_URL/transactions/sync" \
  -H "Authorization: Bearer $FINANCIAL_API_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"item_id":"example-item-id","count":500}'
```

## Dokploy (Docker Compose)

1. Create a **Docker Compose** service from this repo and select the branch
   containing this code. Set the Compose path to **`compose.yaml`** at the repo
   root. It builds the existing `Dockerfile` with context `.`.
2. Set the `FINANCIAL_` environment variables in Dokploy. This Compose file
   explicitly forwards those names (not the legacy `SPENDY_` aliases or raw JSON).
   Set `FINANCIAL_API_TOKEN`, `FINANCIAL_PLAID_CLIENT_ID`, `FINANCIAL_PLAID_SECRET`,
   and `FINANCIAL_PLAID_ITEMS_B64`; Compose refuses to start if any is empty.
   Set `FINANCIAL_PLAID_ENVIRONMENT=production` for real bank connections; the
   default is sandbox. For encrypted tokens also set `FINANCIAL_ENCRYPTION_KEY`.
   No build-time secrets, DB, volume, or service dependencies are needed. Paste
   the single-line base64 export without quote wrappers; do not paste raw JSON.
3. Configure a domain with HTTPS routing to service **`financial-api`**, container
   port **8000**. The service uses **`expose`**, not a host `ports` binding, and
   joins **`dokploy-network`** declared with **`external: true`**. That network
   must already exist on the Dokploy host; Compose does not create it. Dokploy's
   proxy handles public access. Do not publish port 8000 on the host.
4. Deploy and verify `/health`, then authenticated `/items`, `/accounts`, and an
   initial `/transactions/sync` for **each** configured item. Health alone does
   not verify Plaid credentials. Verify that unauthenticated data requests fail.
5. Store the HTTPS base URL and bearer token in Shopped's server-side secrets.
   Leave scheduling/storage/cursor commits in Shopped; nothing runs on a timer here.

The image runs as non-root UID 10001, uses a frozen dependency lock, disables
access logs, and has an HTTP health check. It writes no persistent state. There
is no local rate limiter: apply request-size/time/rate limits and (where feasible)
an IP allowlist at the reverse proxy. The API also caps sync bodies at 32 KiB.
Do not log request/response bodies or authorization headers in Dokploy/Traefik.
No CORS is enabled; this is a server-to-server API, not a browser integration.
Docker build and live Plaid verification must be completed in your deployment
environment; local tests do not establish that real bank connections are healthy.
