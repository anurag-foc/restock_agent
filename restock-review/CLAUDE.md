# AI Assistant Instructions — restock-review

The Production Manager's approval UI for the Inventory Intelligence pipeline.
**Read [`README.md`](./README.md) first** — it covers the pages, the two API
endpoints (only one of which writes), and the four gotchas that cause real
bugs here (disabled analytics cache, no `refetch()`, `sql.string()` param
wrapping).

Project-wide context and the architectural invariants live in the repo root's
[`../CLAUDE.md`](../CLAUDE.md); the agent design rationale is in
[`../docs/agent_bricks_mapping.md`](../docs/agent_bricks_mapping.md).

**The one rule to not break here:** this app is a human's decision surface. It
pre-validates and triggers the `restock_decision` job; it does not make restock
decisions, call the Supervisor Agent, or write quote data. The single
deliberate exception is `POST /api/lines/:lineKey/complete`, a human's
deterministic `FULFILLING → COMPLETED` status flip. Everything the *agent* does
goes through the `mcp-inventory-actions` MCP app.

<!-- appkit-instructions-start -->
## Databricks AppKit

This project uses Databricks AppKit packages. For AI assistant guidance on using these packages, refer to:

- **@databricks/appkit** (Backend SDK): [./node_modules/@databricks/appkit/CLAUDE.md](./node_modules/@databricks/appkit/CLAUDE.md)
- **@databricks/appkit-ui** (UI Integration, Charts, Tables, SSE, and more.): [./node_modules/@databricks/appkit-ui/CLAUDE.md](./node_modules/@databricks/appkit-ui/CLAUDE.md)

### Databricks Skills

For enhanced AI assistance with Databricks CLI operations, authentication, data exploration, and app development, install the Databricks skills:

```bash
databricks aitools install
```
<!-- appkit-instructions-end -->
