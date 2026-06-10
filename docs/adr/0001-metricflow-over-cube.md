# ADR 0001 — MetricFlow over Cube for the semantic layer

**Status:** accepted (2026-06-09)

## Context

The stack needs a governed metrics layer between the dbt marts and the MCP server. The
two candidates evaluated were MetricFlow (dbt Labs, BSL; `dbt-metricflow` package) and
Cube (Cube Dev; Node service with SQL/REST/GraphQL APIs).

## Decision

MetricFlow. It installs into the same Python environment as dbt and the MCP server,
defines metrics in dbt-project YAML, and queries from the project directory with no
separate service. One process, one lockfile, runs in CI.

## Consequences

- Queries go through the `mf` CLI (~2 s per query for a dbt parse). A long-running
  deployment would cache the parsed manifest.
- Metric definitions live in `warehouse/models/semantic/*.yml` next to the models they
  govern, versioned with the transformations.
- Cube remains the alternative if the consumer is a BI tool that wants a SQL/REST
  interface; swapping would change the server's query path, not the dbt models.
