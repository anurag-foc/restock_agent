# restock-review

The **Production Manager's approval surface** for the Inventory Intelligence
pipeline: an AppKit (Node/React) Databricks App, deep-linked from the Teams
Adaptive Card, where a PM reviews the Supervisor Agent's recommendation and
decides on it.

Deployed as part of the parent bundle
(`../resources/apps/restock_review_app.yml`), not separately.

## Pages and data

| Page | Query | Purpose |
|---|---|---|
| `PendingQuotesPage` | `pending_quotes.sql` | Quotes awaiting a decision |
| `QuoteDetailPage` | `quote_header.sql`, `quote_lines.sql` | One quote: the intelligence report plus its part-lines |
| `FulfillingOrdersPage` | `fulfilling_lines.sql` | Approved lines in flight, to mark delivered |

`client/src/components/IntelligenceReport.tsx` parses the Supervisor's
free-text `summary_report` into the structured OUTPUT CONTRACT sections the
detail page renders.

## What the app does and does not write

A PM decides **per part-line** — `fact_restock_request`'s grain is one row per
part-line, and `REQUEST_STATUS_KEY`/`DECISION_DATE_KEY`/`CONFIRMED_QTY`/`NOTE`
are all per-line — but decisions are **staged in the UI and submitted as one
batch**:

- `POST /api/quotes/:quoteId/decisions` takes `{lineKey, decision, note}[]`,
  pre-validates, and triggers the `restock_decision` job once with a
  `decisions_json` array. **It does not write.** The status write and the
  fulfillment turn happen in that job, because the Databricks Apps reverse
  proxy hard-caps requests at 120s and a cold Supervisor+Genie round-trip was
  measured at ~110s.
- `POST /api/lines/:lineKey/complete` **does write**, deliberately: it flips a
  line `FULFILLING → COMPLETED` and appends to `NOTE`. There is no LLM step and
  no guardrail in recording that a delivery arrived, so a job would buy nothing
  but latency. Idempotent — only acts on a line currently `FULFILLING`, and
  appends to `NOTE` rather than overwriting so the approval-stage note
  survives.

## Gotchas that will bite you

- **Analytics caching is deliberately disabled** (`cache: { enabled: false }`
  in `server/server.ts`). The default shared cache served stale rows after a
  decision was written — which on an approval screen means telling a PM that a
  line they just approved is still pending.
- **`useAnalyticsQuery` has no `refetch()`.** The UI forces a refresh by
  remounting via a changing `key`.
- **Analytics query params must be wrapped** (`sql.string(...)`). The wire
  format is `{"__sql_type":"STRING","value":"..."}`; a bare string is rejected
  server-side.
- The app **no longer hosts the Supervisor's action tools** — those moved to
  the `mcp-inventory-actions` app.

Local dev: `npm run dev` (port 8000).

---

> ## ⚠️ Everything below is unmodified AppKit scaffold documentation
>
> Generic setup/auth/deploy instructions from the AppKit template, never
> tailored to this app. Still broadly accurate for tooling and local auth;
> it says nothing about what this app actually does.

---

# restock-review

A Databricks App powered by [AppKit](https://developers.databricks.com/docs/appkit/v0/), featuring React, TypeScript, and Tailwind CSS.

**Enabled plugins:**
- **Analytics** -- SQL query execution against Databricks SQL Warehouses
- **Server** -- Express HTTP server with static file serving and Vite dev mode

## Prerequisites

- Node.js v22+ and npm
- Databricks CLI (for deployment)
- Access to a Databricks workspace

## Databricks Authentication

### Local Development

For local development, configure your environment variables by creating a `.env` file:

```bash
cp .env.example .env
```

Edit `.env` and set the environment variables you need:

```env
DATABRICKS_HOST=https://your-workspace.cloud.databricks.com
DATABRICKS_APP_PORT=8000
# ... other environment variables, depending on the plugins you use
```

### CLI Authentication

The Databricks CLI requires authentication to deploy and manage apps. Configure authentication using one of these methods:

#### OAuth U2M

Interactive browser-based authentication with short-lived tokens:

```bash
databricks auth login --host https://your-workspace.cloud.databricks.com
```

This will open your browser to complete authentication. The CLI saves credentials to `~/.databrickscfg`.

#### Configuration Profiles

Use multiple profiles for different workspaces:

```ini
[DEFAULT]
host = https://dev-workspace.cloud.databricks.com

[production]
host = https://prod-workspace.cloud.databricks.com
client_id = prod-client-id
client_secret = prod-client-secret
```

Deploy using a specific profile:

```bash
databricks bundle deploy --profile production
```

**Note:** Personal Access Tokens (PATs) are legacy authentication. OAuth is strongly recommended for better security.

## Getting Started

### Install Dependencies

```bash
npm install
```

### Development

Run the app in development mode with hot reload:

```bash
npm run dev
```

The app will be available at the URL shown in the console output.

### Build

Build both client and server for production:

```bash
npm run build
```

This creates:

- `dist/server.js` - Compiled server bundle
- `client/dist/` - Bundled client assets

### Production

Run the production build:

```bash
npm start
```

## Code Quality

There are a few commands to help you with code quality:

```bash
# Type checking
npm run typecheck

# Linting
npm run lint
npm run lint:fix

# Formatting
npm run format
npm run format:fix
```

## Deployment with Databricks Asset Bundles

### 1. Configure Bundle

Update `databricks.yml` with your workspace settings:

```yaml
targets:
  default:
    workspace:
      host: https://your-workspace.cloud.databricks.com
```

Make sure to replace all placeholder values in `databricks.yml` with your actual resource IDs.

### 2. Deploy

Deploy and start the app with a single command:

```bash
databricks apps deploy
```

`databricks apps deploy` validates the project, deploys it, starts the app, and prints its URL.

### Deploy to Production

1. Configure the production target in `databricks.yml`
2. Deploy to production:

```bash
databricks apps deploy -t prod
```

> **Restarting a stopped app:** apps stop after a period of inactivity. To start one again without redeploying, run `databricks apps start <APP_NAME>`.

## Project Structure

```
* client/          # React frontend
  * src/           # Source code
  * public/        # Static assets
* server/          # Express backend
  * server.ts      # Server entry point
  * routes/        # Routes
* shared/          # Shared types
* config/          # Configuration
  * queries/       # SQL query files
* databricks.yml   # Bundle configuration
* app.yaml         # App configuration
* .env.example     # Environment variables example
```

## Tech Stack

- **Backend**: Node.js, Express
- **Frontend**: React.js, TypeScript, Vite, Tailwind CSS, React Router
- **UI Components**: Radix UI, shadcn/ui
- **Databricks**: AppKit SDK
