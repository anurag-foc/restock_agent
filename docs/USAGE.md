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
- Access to the target workspace: `https://dbc-663771e2-fb94.cloud.databricks.com`

## 2. Authenticate the CLI (one-time, and whenever the token expires)

This project uses a named profile, `anurag-r`. Log in via OAuth (opens a browser):

```bash
databricks auth login --profile anurag-r --host https://dbc-663771e2-fb94.cloud.databricks.com
```

Verify the profile is valid:

```bash
databricks auth profiles
# Name                Host                                            Valid
# anurag-r (Default)  https://dbc-663771e2-fb94.cloud.databricks.com  YES
```

If you ever see `invalid_grant: Refresh token is invalid` on a `bundle validate` /
`bundle deploy` / `bundle run`, re-run the `auth login` command above — the token expired,
nothing else is wrong.

> All commands below assume `anurag-r` is your **default** CLI profile (as configured).
> If you use a different profile name, add `--profile <name>` to every command.

## 3. Bundle targets

Two targets are defined in `databricks.yml`:

| Target | Mode | Use for |
|---|---|---|
| `dev` (default) | `development` | Everyday iteration. Resources are prefixed/tagged per-user by Databricks so multiple people can deploy the same bundle without colliding. |
| `prod` | `production` | The real, shared deployment once a piece is stable. Deploys under a fixed `root_path` (`/Workspace/Shared/.bundle/agentic_restock/prod`) instead of a personal path. |

Every command below takes `-t dev` or `-t prod`. **Default to `dev` unless you explicitly
mean to touch production.**

Catalog/schema are bundle variables (`var.catalog` / `var.schema`, default
`ab_training.agentic_restock` — see `databricks.yml`). Override per-invocation if needed:

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

This uploads the bundle's files to `/Workspace/Users/<you>/.bundle/agentic_restock/dev/`
(dev target) and creates/updates each resource under `resources/`.

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
      name: "[${bundle.target}] <Human Readable Name>"
      description: >-
        What this job does and why.
      tasks:
        - task_key: <task_key>
          notebook_task:            # or python_wheel_task / spark_python_task, etc.
            notebook_path: ../../notebooks/<file>.ipynb
      schedule:                     # omit for on-demand jobs like schema_bootstrap
        quartz_cron_expression: "0 0 * * * ?"   # hourly, for the Lakeflow trigger job
        timezone_id: UTC
      tags:
        project: agentic_restock
```

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
| Changes not showing up after `deploy` | Confirm you deployed the target you're looking at (`dev` resources live under your personal `/Workspace/Users/<you>/...` path, not the shared `prod` path). |
| Want a clean slate | `databricks bundle destroy -t dev` then `databricks bundle deploy -t dev` again. |
| Multiple people deploying `dev` and stepping on each other | Expected with `mode: development` — each user's deploy is isolated under their own path. Don't share a `dev` deployment; use `prod` for anything shared. |

## 8. Reference

- [Databricks Asset Bundles overview](https://docs.databricks.com/en/dev-tools/bundles/index.html)
- [Bundle YAML reference](https://docs.databricks.com/en/dev-tools/bundles/reference.html)
- [Databricks CLI bundle command reference](https://docs.databricks.com/en/dev-tools/cli/bundle-commands.html)
