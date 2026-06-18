# dbt-semantic-mcp

An MCP server that answers KPI queries from governed MetricFlow metrics over a DuckDB warehouse with dbt medallion marts.

## What it does

A small warehouse stack, end to end: synthetic seed data → dbt staging/intermediate/mart
models (39 tests) → 9 governed metrics defined once in MetricFlow YAML → a Python MCP
server with three tools (`list_metrics`, `query_metric`, `describe_lineage`). An MCP
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

## How it works

- `scripts/generate_seed.py` — deterministic synthetic data (RNG seed 42); the committed
  CSVs are its output, verified by a test.
- `warehouse/` — dbt project: staging views → `int_order_items_pricing` → marts
  (`fct_orders`, `fct_order_items`, `dim_customers`, `dim_products`) + semantic YAML.
- `src/dbt_semantic_mcp/` — the MCP server. Queries shell out to the `mf` CLI; lineage
  parses `target/manifest.json`.
- `tests/` — generator reproducibility, metric-vs-direct-SQL cross-check, lineage,
  stdio round-trip with a real MCP client.

## Status

0.1.0 (SemVer). Shipped: the full pipeline — seeds, 11 dbt models with 39 tests, 9
MetricFlow metrics, and the MCP server with `list_metrics`, `query_metric`, and
`describe_lineage` over stdio; 8 pytest tests including an MCP stdio round-trip.

Boundaries:

- Seed data, not production scale: 4,000 synthetic orders in one DuckDB file. The dbt
  patterns transfer; the operational concerns of a cloud warehouse (Snowflake/BigQuery),
  orchestration (Airflow), and Spark-scale pipelines are scoped here, not built.
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
