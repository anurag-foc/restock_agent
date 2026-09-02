# Usage Guide — Databricks Asset Bundles (DAB)

This project is deployed to Databricks exclusively via **Databricks Asset Bundles**
(DAB). There is no manual notebook upload, no click-ops job creation — everything in
`resources/` is declared as code in `databricks.yml` + `resources/**/*.yml` and pushed
to the workspace with the `databricks bundle` CLI.

This doc is the day-to-day runbook. For *what* the project does, see the root
[`README.md`](../README.md); for *why* it's designed this way, see
[`architecture.md`](architecture.md).

---

## 1. Prerequisites

- Databricks CLI **v0.205+** (this repo was set up against v1.8.0):
  ```bash
  databricks --version
  ```
  Install/upgrade: https://docs.databricks.com/en/dev-tools/cli/install.html
- `uv` for local Python dev (running tests, linting agent code before it touches Databricks):
  ```bash
  uv --version
  ```
- Access to the target workspace: `https://dbc-371e9acf-b9ec.cloud.databricks.com`

## 2. Authenticate the CLI (one-time, and whenever the token expires)

This project uses a named profile, `anurag-r`. Log in via OAuth (opens a browser):

```bash
databricks auth login --profile anurag-r --host https://dbc-371e9acf-b9ec.cloud.databricks.com
```

Verify the profile is valid:

```bash
databricks auth profiles
# Name                Host                                            Valid
# anurag-r (Default)  https://dbc-371e9acf-b9ec.cloud.databricks.com  YES
```

If you ever see `invalid_grant: Refresh token is invalid` on a `bundle validate` /
`bundle deploy` / `bundle run`, re-run the `auth login` command above — the token expired,
nothing else is wrong.

> All commands below assume `anurag-r` is your **default** CLI profile (as configured).
> If you use a different profile name, add `--profile <name>` to every command.

## 3. Bundle targets

Two targets are defined in `databricks.yml`:

| Target | Mode | Name prefix | Deploys under | Use for |
|---|---|---|---|---|
| `dev` (default) | *(none)* | `[dev] ` | `/Workspace/Shared/.bundle/agentic_restock/dev` | Everyday iteration. |
| `prod` | `production` | `[prod] ` | `/Workspace/Shared/.bundle/agentic_restock/prod` | The real, shared deployment once a piece is stable. |

Both targets deploy to a shared path and carry a fixed target prefix, so no deployed
resource name, tag, or path contains the deploying user's name. Neither target is
per-user isolated as a result — see §6 for why `dev` skips `mode: development`.

Every command below takes `-t dev` or `-t prod`. **Default to `dev` unless you explicitly
mean to touch production.**

Catalog/schema are bundle variables (`var.catalog` / `var.schema`, default
`gold_dev.supply_chain_analytics` — see `databricks.yml`). Override per-invocation if needed:

```bash
databricks bundle deploy -t dev --var="catalog=my_sandbox,schema=agentic_restock_dev"
```

## 4. Core commands

Run these from the repo root (where `databricks.yml` lives).

**Validate** — checks YAML syntax and resolves it against the live workspace (catches
bad notebook paths, invalid cluster specs, etc.) without changing anything:

```bash
databricks bundle validate -t dev
```

**Deploy** — syncs all files and creates/updates the declared resources (Jobs, Apps, ...)
in the workspace:

```bash
databricks bundle deploy -t dev
```

This uploads the bundle's files to `/Workspace/Shared/.bundle/agentic_restock/dev/` (dev
target) and creates/updates each resource under `resources/`. Both targets deploy under
`/Workspace/Shared` rather than the default per-user home folder, because deployed notebook
paths are displayed verbatim on every job task and job run — see the `root_path` comment in
`databricks.yml`. The trade-off is that a `dev` deploy is *not* isolated per person: the CLI
warns that a `/Workspace/Shared` root is writable by all workspace users, and two people
deploying `dev` will overwrite each other.

**Run a job** — triggers a deployed job by its resource key (the key under
`resources.jobs.<key>` in the YAML, e.g. `schema_bootstrap`):

```bash
databricks bundle run schema_bootstrap -t dev
```

Add `--no-wait` to fire-and-forget instead of streaming logs until completion.

**Summary** — shows what's currently deployed for a target and links to open resources in
the workspace UI:

```bash
databricks bundle summary -t dev
```

**Destroy** — tears down everything the bundle deployed for a target (does **not** touch
the Unity Catalog tables/schema themselves, only the Jobs/Apps/notebooks it created):

```bash
databricks bundle destroy -t dev
```

## 5. Day-to-day workflow

1. Edit code/YAML locally (notebooks under `notebooks/`, job/app resources under
   `resources/`, Python agent logic under `src/agentic_restock/`).
2. Run local checks before touching the workspace:
   ```bash
   uv run pytest -q
   uv run ruff check .
   ```
3. `databricks bundle validate -t dev`
4. `databricks bundle deploy -t dev`
5. `databricks bundle run <job_key> -t dev` to exercise it, or open the workspace UI
   (Workflows / Apps) via the link from `databricks bundle summary -t dev`.
6. Once verified, deploy to `prod` the same way with `-t prod`.

## 6. Adding a new resource (next roadmap items)

Each roadmap item (Lakeflow trigger job, Genie/Supervisor/Restock agents, review app) adds
a new file under `resources/jobs/` or `resources/apps/`, following the existing pattern in
`resources/jobs/schema_bootstrap_job.yml`:

```yaml
resources:
  jobs:
    <resource_key>:
      name: "<Human Readable Name>"
      description: >-
        What this job does and why.
      tasks:
        - task_key: <task_key>
          notebook_task:            # or python_wheel_task / spark_python_task, etc.
            notebook_path: ../../notebooks/<file>.ipynb
      schedule:                     # omit for on-demand jobs like schema_bootstrap
        quartz_cron_expression: "0 0 7,15 * * ?" # the Lakeflow trigger job's cadence
        timezone_id: UTC
      tags:
        project: agentic_restock
```

Don't hand-prefix `name` with `[${bundle.target}]` — each target in `databricks.yml`
declares `presets.name_prefix` (`"[dev] "` / `"[prod] "`), which the CLI prepends to
every deployed resource name for you, Genie Space included. A manual prefix on top of
that just doubles up.

The `dev` target intentionally does **not** set `mode: development`. Dev mode prepends
`[dev <deploying user>]` to every resource name and adds a `dev: <deploying user>` tag
to every job, and it refuses any `name_prefix` that omits that username — so it's the
one setting that forces a personal name into the workspace UI. The behaviours we
actually want from it are already declared explicitly: the Lakeflow job sets
`pause_status: PAUSED` on its schedule, and there are no DLT pipelines needing a
development flag. If more than one person ever deploys the `dev` target to the same
workspace, give each their own target (or re-enable dev mode) so names don't collide.

Notebook/file paths in a resource YAML are resolved **relative to that YAML file's own
location**, not the bundle root — hence `../../notebooks/...` from `resources/jobs/`.

`databricks.yml` already globs `resources/*.yml` and `resources/**/*.yml`, so a new file
dropped into `resources/jobs/` or `resources/apps/` is picked up automatically — no need
to edit `databricks.yml` itself.

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `invalid_grant: Refresh token is invalid` | Re-run `databricks auth login --profile anurag-r` (§2). |
| `Error: notebook not found` on deploy | Check the `notebook_path` is relative to the *resource YAML's* folder, not the repo root (§6). |
| Changes not showing up after `deploy` | Confirm you deployed the target you're looking at — `dev` and `prod` resources differ only by the `[dev]`/`[prod]` name prefix and their `/Workspace/Shared/.bundle/agentic_restock/<target>/` path. |
| Want a clean slate | `databricks bundle destroy -t dev` then `databricks bundle deploy -t dev` again. |
| Multiple people deploying `dev` and stepping on each other | Neither target is per-user isolated (§3). Give each person their own target with its own `name_prefix` and `root_path` instead of sharing `dev`. |

## 8. Reference

- [Databricks Asset Bundles overview](https://docs.databricks.com/en/dev-tools/bundles/index.html)
- [Bundle YAML reference](https://docs.databricks.com/en/dev-tools/bundles/reference.html)
- [Databricks CLI bundle command reference](https://docs.databricks.com/en/dev-tools/cli/bundle-commands.html)
