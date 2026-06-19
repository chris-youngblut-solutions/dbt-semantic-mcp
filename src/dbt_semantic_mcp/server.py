"""MCP server exposing the governed semantic layer over stdio.

Tools: list_metrics, query_metric, query_metric_via_backend, active_backend,
describe_lineage. The natural-language half of an NL KPI query is the MCP host's
job — the host picks metrics and group-bys from the catalog the tools return;
the server only answers from the governed definitions.

Two query paths answer the *same* governed questions:

- ``query_metric`` — MetricFlow (the ``mf`` CLI) over the local dbt project.
- ``query_metric_via_backend`` — Ibis over a configurable SQL backend
  (DuckDB local by default; Postgres / Snowflake / BigQuery when configured).
  This is the connector seam: the agent calls one tool and never learns which
  tier answered. Swap the backend, keep the interface; degrades to local DuckDB.

Run: uv run dbt-semantic-mcp
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from dbt_semantic_mcp import connector, warehouse

mcp = FastMCP("dbt-semantic-mcp")


@mcp.tool()
def list_metrics() -> list[dict[str, Any]]:
    """List the governed metrics: name, label, type, description, and the
    group-bys each metric can be sliced by (e.g. metric_time, customer__region).
    Call this first to pick the metric and group-by for a KPI question."""
    return warehouse.list_metrics()


@mcp.tool()
def query_metric(
    metrics: list[str],
    group_by: list[str] | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    order_by: list[str] | None = None,
    limit: int | None = None,
) -> list[dict[str, str]]:
    """Query one or more governed metrics through MetricFlow.

    - metrics: metric names from list_metrics (e.g. ["revenue"]).
    - group_by: optional group-bys, e.g. ["metric_time__month"] or
      ["customer__region"]. Time group-bys take a grain suffix:
      __day, __week, __month, __quarter, __year.
    - start_time / end_time: optional inclusive ISO dates (e.g. "2025-01-01").
    - order_by: optional; prefix with "-" for descending, e.g. ["-revenue"].
    - limit: optional row cap.

    Returns rows as dicts keyed by column name. Values are returned exactly as
    the warehouse computed them, as strings."""
    return warehouse.query_metrics(
        metrics=metrics,
        group_by=group_by,
        start_time=start_time,
        end_time=end_time,
        order_by=order_by,
        limit=limit,
    )


@mcp.tool()
def query_metric_via_backend(
    metrics: list[str],
    group_by: list[str] | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    order_by: list[str] | None = None,
    limit: int | None = None,
) -> list[dict[str, str]]:
    """Query governed metrics through the configurable SQL backend (Ibis).

    Same metric/group-by inputs and same row shape as ``query_metric`` — the
    difference is the engine. The backend is selected by the server's
    ``DBT_SEMANTIC_MCP_BACKEND`` env (default ``duckdb``, the local OSS
    fallback; ``postgres`` / ``snowflake`` / ``bigquery`` for hosted tiers).
    Credentials are read from the environment by the backend, never passed
    here. The metric definitions are still the governed semantic manifest, so
    swapping the backend does not change what a metric means — only where it
    runs. Use this when the warehouse lives in a hosted engine rather than the
    local DuckDB file."""
    return connector.query_metric_ibis(
        metrics=metrics,
        group_by=group_by,
        start_time=start_time,
        end_time=end_time,
        order_by=order_by,
        limit=limit,
    )


@mcp.tool()
def active_backend() -> dict[str, str]:
    """The SQL backend tier the ``query_metric_via_backend`` tool will use
    (``duckdb`` local, or a configured hosted engine). Lets a host confirm which
    tier is wired without leaking credentials."""
    return {"backend": connector.backend_name()}


@mcp.tool()
def describe_lineage(node_name: str) -> dict[str, Any]:
    """Upstream and downstream dbt nodes for a model, seed, or metric name
    (a metric resolves to the mart its measures read from). Use it to answer
    "where does this number come from"."""
    return warehouse.describe_lineage(node_name)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
