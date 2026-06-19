# dbt-semantic-mcp

An MCP server that answers KPI queries from governed MetricFlow metrics over a DuckDB warehouse with dbt medallion marts.

## What it does

A small warehouse stack, end to end: synthetic seed data → dbt staging/intermediate/mart
models (39 tests) → 9 governed metrics defined once in MetricFlow YAML → a Python MCP
server (`list_metrics`, `query_metric`, `query_metric_via_backend`, `active_backend`,
`describe_lineage`). Metrics answer two ways: through MetricFlow, or through an Ibis
connector over a swappable SQL backend (DuckDB local by default; Postgres / Snowflake /
BigQuery when configured) — see [Multi-backend](#multi-backend-ibis). An MCP
host (Claude Desktop, Claude Code, or any MCP client) answers natural-language KPI
questions by picking metrics from the catalog; every number comes from the same governed
definitions an analyst would query. No SQL is composed from model or user input. The
analyst CLI and the agent read the same MetricFlow YAML definition, so a metric is single-sourced.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/). Python 3.12 and all packages are pinned by
the lockfile.

```sh
git clone https://github.com/chris-youngblut-solutions/dbt-semantic-mcp && cd dbt-semantic-mcp
uv sync --all-extras
cd warehouse
uv run dbt build          # seeds + 11 models + 39 tests
uv run mf query --metrics revenue,order_count --group-by metric_time__year --decimals 0
cd ..
uv run pytest             # 8 tests, incl. an MCP stdio round-trip
```

`dbt build` output ends `PASS=55 WARN=0 ERROR=0`. The `mf query` returns:

```
metric_time__year      revenue    order_count
-------------------  ---------  -------------
2024-01-01T00:00:00    1198339            706
2025-01-01T00:00:00    3304658           1953
```

Register the MCP server with a host (stdio transport):

```json
{
  "mcpServers": {
    "dbt-semantic-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/dbt-semantic-mcp", "dbt-semantic-mcp"]
    }
  }
}
```

Example exchange: ask the host *"what was revenue by region last year?"* — it calls
`list_metrics`, finds `revenue` with group-by `customer__region`, then calls
`query_metric(metrics=["revenue"], group_by=["customer__region"], start_time="2025-01-01",
end_time="2025-12-31")` and reads back four rows. `describe_lineage("revenue")` answers
"where does this number come from": `fct_order_items ← int_order_items_pricing ←
stg_order_items/stg_orders/stg_products ← seeds`.

### The metrics

revenue, units_sold, gross_profit, order_count, average_order_value (ratio),
new_customers, on_time_shipments, shipped_orders, on_time_shipment_rate (ratio).
Definitions in `warehouse/models/semantic/metrics.yml`; the business rules (revenue
counts completed orders only; a new customer is a first non-cancelled order) live in
the YAML and the mart SQL, not in the server.

## Multi-backend (Ibis)

The server exposes a second query path, `query_metric_via_backend`, that answers the
same governed-metric questions through [Ibis](https://ibis-project.org/) instead of the
`mf` CLI. The point is one MCP interface over a swappable SQL engine:

- **DuckDB (default, local)** — the degrades-to-local fallback. No network, no
  credentials; reads the dbt-built `warehouse.duckdb`.
- **Postgres / Snowflake / BigQuery (hosted)** — same tool, same inputs, same row shape.
  Selected with `DBT_SEMANTIC_MCP_BACKEND`; the agent never learns which tier answered.

The aggregations are derived from the **governed semantic manifest** (the same
`semantic_manifest.json` the MetricFlow path reads), not composed from input — swapping
the backend changes *where* a metric runs, not *what it means*. `query_metric_via_backend`
and `query_metric` return identical numbers for the same metric (a test asserts this on
DuckDB). `active_backend` reports the configured tier.

Credentials are read from the environment / `~/.netrc` / ADC by each Ibis backend and are
never passed as arguments or baked into code:

```sh
# default — local DuckDB, no creds
uv run dbt-semantic-mcp

# hosted Postgres (password from ~/.pgpass / PG* env, not argv)
DBT_SEMANTIC_MCP_BACKEND=postgres PGHOST=… PGDATABASE=analytics uv run dbt-semantic-mcp
```

The single-table Ibis path handles the simple/same-mart ratio metrics and local /
time group-bys; cross-mart ratios (e.g. `average_order_value`) and cross-entity
group-bys (e.g. `customer__region`) stay on the MetricFlow path, which remains the
authority and is rejected with a clear message rather than guessed.

## How it works

- `scripts/generate_seed.py` — deterministic synthetic data (RNG seed 42); the committed
  CSVs are its output, verified by a test.
- `warehouse/` — dbt project: staging views → `int_order_items_pricing` → marts
  (`fct_orders`, `fct_order_items`, `dim_customers`, `dim_products`) + semantic YAML.
- `src/dbt_semantic_mcp/` — the MCP server. `query_metric` shells out to the `mf` CLI;
  `query_metric_via_backend` composes the query with Ibis over the configured backend
  (`connector.py`); lineage parses `target/manifest.json`.
- `tests/` — generator reproducibility, metric-vs-direct-SQL cross-check, lineage,
  stdio round-trip with a real MCP client.

## Status

0.1.0 (SemVer). Shipped: the full pipeline — seeds, 11 dbt models with 39 tests, 9
MetricFlow metrics, and the MCP server with `list_metrics`, `query_metric`,
`query_metric_via_backend`, `active_backend`, and `describe_lineage` over stdio; pytest
suite including MCP stdio round-trips for both query paths and a backend-swap equivalence
check.

Boundaries:

- Seed data, not production scale: 4,000 synthetic orders in one DuckDB file. The dbt
  patterns transfer; orchestration (Airflow) and Spark-scale pipelines are scoped here,
  not built.
- The Ibis multi-backend path is wired and tested against local DuckDB (and a simulated
  hosted DuckDB for the swap-equivalence test). The Postgres / Snowflake / BigQuery
  factories read credentials from the environment and are covered by mocked tests; they
  have not been run against live hosted warehouses here.
- The metric set is the demo set (9 metrics, one domain). Adding a metric is YAML, not
  server code: add a measure/metric in `warehouse/models/semantic/`, then
  `uv run dbt parse && uv run mf validate-configs` — the server picks it up from the
  regenerated manifest.
- `query_metric` exposes metrics, group-bys, time bounds, ordering, and a row limit —
  not MetricFlow's `--where` filter syntax.
- The group-by list in `list_metrics` is a one-hop join approximation; `mf query` is the
  authority and returns valid candidates on a miss.
- Server transport is stdio only.

## Development

```sh
pre-commit install   # one-time after clone
just check           # fmt + lint + test
```

## License

Licensed under either of:

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE) or
  <http://www.apache.org/licenses/LICENSE-2.0>)
- MIT license ([LICENSE-MIT](LICENSE-MIT) or
  <http://opensource.org/licenses/MIT>)

at your option.

### Contribution

Unless you explicitly state otherwise, any contribution intentionally
submitted for inclusion in this project by you, as defined in the
Apache-2.0 license, shall be dual licensed as above, without any
additional terms or conditions.
