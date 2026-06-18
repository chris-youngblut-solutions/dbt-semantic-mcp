# Design

Architecture, pinned versions, and design decisions for the dbt + MetricFlow + MCP warehouse stack.

## Architecture

```
scripts/generate_seed.py        deterministic synthetic CSVs (committed)
        │
        ▼
warehouse/  (dbt + DuckDB)
  seeds/raw_*               5 seed tables
  models/staging/           views: rename/cast, 1:1 with seeds
  models/intermediate/      int_order_items_pricing: line revenue/cost/margin
  models/marts/             fct_orders, fct_order_items, dim_customers, dim_products
  models/semantic/          4 semantic models, 9 metrics (MetricFlow YAML)
  models/utilities/         day-grain time spine
        │
        ▼  target/: warehouse.duckdb, manifest.json, semantic_manifest.json
src/dbt_semantic_mcp/  (MCP server, stdio)
  list_metrics      ← semantic_manifest.json
  query_metric      ← subprocess: mf query --csv
  describe_lineage  ← manifest.json parent_map/child_map
```

The natural-language half of an NL KPI query belongs to the MCP host: the host reads
the metric catalog (names, descriptions, group-bys) and picks the call. The server only
executes governed definitions. A hallucinated metric name fails with an `mf` error that
lists valid candidates, rather than querying wrong SQL.

## Pinned versions (resolved 2026-06-09, locked in uv.lock)

| Component | Version |
|---|---|
| Python | 3.12.13 (uv-pinned; `requires-python >=3.12,<3.13`) |
| dbt-core | 1.11.11 |
| dbt-duckdb | 1.10.1 |
| dbt-metricflow | 0.13.0 (metricflow 0.211.0) |
| duckdb | 1.5.3 |
| mcp | 1.27.2 |

## Decisions

- **MetricFlow over Cube.** Both define governed metrics over dbt models. MetricFlow
  installs into the same Python environment and runs from the dbt project with no
  separate service; Cube is a Node service with its own deployment surface. For a
  single-process local stack, MetricFlow. Cube is the alternative if the consumer is a
  BI tool wanting a SQL/REST interface rather than a CLI/library.
- **Queries shell out to the `mf` CLI** rather than importing MetricFlow's Python
  internals. The CLI is the documented interface; the internals are not a stable API.
  Cost: ~2 s per query for a dbt parse. Acceptable here; a long-running deployment
  would cache the parsed manifest.
- **Synthetic data, generator committed next to its output.** A test regenerates into a
  temp dir and compares SHA-256 against the committed CSVs, so data provenance is
  checked in CI, not asserted in prose.
- **Revenue counts completed orders only.** Encoded once: `fct_order_items` filters to
  completed; the metric reads the mart. Returned/cancelled orders stay visible in
  `fct_orders` (all statuses) for operational metrics like on-time rate.
- **`query_metric` omits MetricFlow's `--where` filter.** The filter syntax takes
  Jinja-templated expressions; passing host-authored strings through would reintroduce
  the freeform-input surface this design avoids. Time bounds, group-bys, ordering, and
  limits cover the demo questions.

## Scope

Built and tested here: dbt modeling and tests, a MetricFlow semantic layer, an MCP
server, CI running the whole chain. Scoped but not built: cloud warehouses
(Snowflake/BigQuery), orchestration (Airflow/Dagster), Spark-scale pipelines,
incremental models, server transports beyond stdio. The README's Limits section is the
user-facing version of this list.
