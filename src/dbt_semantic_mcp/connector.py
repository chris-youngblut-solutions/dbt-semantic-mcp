"""Multi-backend Ibis connector — the "swap the backend, same interface" spine.

This is a second, optional query path that sits *beside* the MetricFlow path in
``warehouse.py``. It answers the same governed-metric questions, but composes the
query with Ibis so the *same* interface runs against several SQL engines:

- **duckdb** (default, local OSS) — the degrades-to-local fallback. No network,
  no creds; reads the dbt-built ``warehouse.duckdb`` file.
- **postgres / snowflake / bigquery** (hosted) — same interface, different tier.
  Selected by env; credentials are read from the environment / ``~/.netrc`` by
  the respective Ibis backend, never baked into code or passed on argv.

Governance is preserved: the aggregations are derived from the **governed
semantic manifest** (``semantic_manifest.json``), not from user input. The
caller picks a metric name and group-bys from the catalog; this module maps
those names onto the measure ``agg`` / ``expr`` the dbt project already
declared. No SQL string is composed from caller-supplied text.

The connector seam is what the MCP tool exposes: the agent calls one tool and
never learns which tier answered it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from dbt_semantic_mcp import warehouse

# ibis ships no type stubs; treat its expression / connection objects as opaque
# at the boundary rather than scatter per-line ignores. These aliases keep the
# intent legible (a table, a column expr, a backend) while satisfying strict
# type checking — the runtime objects are the real ibis types.
IbisTable = Any
IbisExpr = Any
IbisBackend = Any

# Backends we know how to wire credentials for. duckdb is the local fallback.
SUPPORTED_BACKENDS = ("duckdb", "postgres", "snowflake", "bigquery")
DEFAULT_BACKEND = "duckdb"

# How each governed measure aggregates. The manifest uses MetricFlow agg names;
# map them to the Ibis reduction method on a column expression.
_AGG_METHODS = {
    "sum": "sum",
    "count": "count",
    "count_distinct": "nunique",
    "min": "min",
    "max": "max",
    "average": "mean",
}


class ConnectorError(RuntimeError):
    """The Ibis connector could not be built or a query could not be composed."""


@dataclass(frozen=True)
class Measure:
    """A governed measure: where it lives and how it aggregates."""

    name: str
    relation: str  # e.g. fct_order_items (unqualified table/view name)
    agg: str
    expr: str | None  # SQL fragment from the dbt model, or None == the column itself


@dataclass(frozen=True)
class MetricDef:
    """A governed metric reduced to the measures the Ibis path needs."""

    name: str
    metric_type: str  # simple | ratio | ...
    measures: list[str]


def backend_name() -> str:
    """The configured backend; ``duckdb`` (local) unless overridden by env."""
    name = os.environ.get("DBT_SEMANTIC_MCP_BACKEND", DEFAULT_BACKEND).strip().lower()
    if name not in SUPPORTED_BACKENDS:
        raise ConnectorError(
            f"unsupported backend {name!r}; choose one of {', '.join(SUPPORTED_BACKENDS)}"
        )
    return name


def _ibis_backend(name: str) -> Any:
    """Resolve the lazy Ibis backend module (e.g. ``ibis.postgres``).

    Factored out so tests can patch *this* (without importing an uninstalled
    hosted backend) and so the credential-handling is the only thing
    ``make_backend`` does.
    """
    import ibis

    return getattr(ibis, name)


def make_backend(name: str | None = None) -> IbisBackend:
    """Build an Ibis backend connection for the configured tier.

    Credentials are *never* taken from arguments. Each hosted backend reads its
    own env vars (and, where supported, ``~/.netrc``); we only pass non-secret
    connection coordinates that are themselves env-sourced. The default,
    ``duckdb``, needs no credentials at all and opens the local warehouse file
    read-only.
    """
    name = name or backend_name()

    if name == "duckdb":
        db_path = os.environ.get("DBT_SEMANTIC_MCP_DUCKDB")
        if not db_path:
            db_path = str(warehouse.warehouse_dir() / "target" / "warehouse.duckdb")
        return _ibis_backend("duckdb").connect(db_path, read_only=True)

    if name == "postgres":
        # libpq reads ~/.pgpass / PG* env for the password; we never pass it.
        return _ibis_backend("postgres").connect(
            host=os.environ.get("PGHOST", "localhost"),
            port=int(os.environ.get("PGPORT", "5432")),
            database=os.environ.get("PGDATABASE", "warehouse"),
            user=os.environ.get("PGUSER", os.environ.get("USER", "")),
        )

    if name == "snowflake":
        # Password / private-key path come from the environment, not argv.
        return _ibis_backend("snowflake").connect(
            account=os.environ["SNOWFLAKE_ACCOUNT"],
            user=os.environ["SNOWFLAKE_USER"],
            password=os.environ.get("SNOWFLAKE_PASSWORD"),
            authenticator=os.environ.get("SNOWFLAKE_AUTHENTICATOR"),
            database=os.environ.get("SNOWFLAKE_DATABASE", "WAREHOUSE"),
            schema=os.environ.get("SNOWFLAKE_SCHEMA", "MAIN"),
            warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE"),
        )

    if name == "bigquery":
        # Application Default Credentials (GOOGLE_APPLICATION_CREDENTIALS) only.
        return _ibis_backend("bigquery").connect(
            project_id=os.environ["BIGQUERY_PROJECT"],
            dataset_id=os.environ.get("BIGQUERY_DATASET", "warehouse"),
        )

    raise ConnectorError(f"no factory for backend {name!r}")  # pragma: no cover


# --- governed-metric resolution (manifest only, never user input) ------------


def _relation_table_name(relation_name: str) -> str:
    """The unqualified table/view name from a manifest ``relation_name``.

    The manifest stores fully-qualified, quoted relations like
    ``"warehouse"."main"."fct_orders"``; we want the leaf ``fct_orders`` so the
    same metric resolves against whatever schema a hosted backend exposes.
    """
    leaf = relation_name.strip().split(".")[-1]
    return leaf.strip().strip('"')


def _measures_by_name(manifest: dict[str, Any]) -> dict[str, Measure]:
    out: dict[str, Measure] = {}
    for sm in manifest["semantic_models"]:
        table = _relation_table_name(sm["node_relation"]["relation_name"])
        for meas in sm["measures"]:
            out[meas["name"]] = Measure(
                name=meas["name"],
                relation=table,
                agg=meas["agg"],
                expr=meas.get("expr"),
            )
    return out


def _metric_defs(manifest: dict[str, Any]) -> dict[str, MetricDef]:
    out: dict[str, MetricDef] = {}
    for metric in manifest["metrics"]:
        measures = [im["name"] for im in metric["type_params"]["input_measures"]]
        out[metric["name"]] = MetricDef(
            name=metric["name"],
            metric_type=metric["type"],
            measures=measures,
        )
    return out


def _time_dim_by_relation(manifest: dict[str, Any]) -> dict[str, str]:
    """Map a relation to its agg-time-dimension column (for time group-bys)."""
    out: dict[str, str] = {}
    for sm in manifest["semantic_models"]:
        table = _relation_table_name(sm["node_relation"]["relation_name"])
        defaults: dict[str, Any] = sm.get("defaults") or {}
        agg_td: str | None = defaults.get("agg_time_dimension")
        if agg_td:
            out[table] = agg_td
    return out


# Time-group-by grain suffixes the caller may append, mapped to a (unit) we
# truncate the timestamp to with Ibis (backend-portable).
_GRAINS = ("day", "week", "month", "quarter", "year")


def _measure_column(table: IbisTable, measure: Measure) -> IbisExpr:
    """Turn a governed measure into an Ibis aggregate expression on ``table``.

    Two shapes cover this demo's governed measures:

    - ``expr`` is a bare column name -> aggregate that column.
    - ``expr`` is a ``case when <cond> then 1 else 0 end`` counter -> rebuild
      it from the columns with Ibis (portable across backends).

    Anything else is rejected rather than guessed — we never compile arbitrary
    SQL text from the manifest into a hosted backend blindly.
    """

    agg_method = _AGG_METHODS.get(measure.agg)
    if agg_method is None:
        raise ConnectorError(f"measure {measure.name!r}: unsupported agg {measure.agg!r}")

    expr = (measure.expr or measure.name).strip()

    # Bare column reference.
    if expr in table.columns:
        col = table[expr]
        return getattr(col, agg_method)()

    # Governed boolean counter: case when <col[=val]> then 1 else 0 end.
    indicator = _case_indicator(table, expr)
    if indicator is not None:
        return getattr(indicator.cast("int64"), agg_method)()

    raise ConnectorError(
        f"measure {measure.name!r}: expr {expr!r} is not a known column or a "
        "supported governed counter; the Ibis path only handles governed "
        "column / case-counter measures"
    )


def _case_indicator(table: IbisTable, expr: str) -> IbisExpr | None:
    """Parse the governed ``case when <cond> then 1 else 0 end`` measures.

    Supported conditions (all governed, from the dbt models):
      - ``status = 'completed'``      -> table.status == 'completed'
      - ``is_first_order``            -> table.is_first_order (boolean column)
      - ``shipped_date is not null``  -> table.shipped_date.notnull()
    """
    low = " ".join(expr.lower().split())
    if not (low.startswith("case when ") and " then 1 else 0 end" in low):
        return None
    cond = expr[len("case when ") :]
    cond = cond[: cond.lower().index(" then 1 else 0 end")].strip()
    cl = cond.lower()

    # <col> is not null
    if cl.endswith("is not null"):
        col = cond[: cl.index("is not null")].strip()
        return table[col].notnull() if col in table.columns else None

    # <col> = '<value>'
    if "=" in cond:
        left, right = cond.split("=", 1)
        col = left.strip()
        val = right.strip().strip("'\"")
        return table[col] == val if col in table.columns else None

    # bare boolean column
    if cond in table.columns:
        return table[cond]

    return None


def _apply_group_by(
    table: IbisTable,
    group_by: list[str],
    time_dim: str | None,
) -> tuple[IbisTable, dict[str, IbisExpr]]:
    """Resolve group-by tokens to Ibis key expressions on ``table``.

    Supported here (single-table, no joins — the marts are pre-joined for the
    demo): ``metric_time[__grain]`` and a bare local dimension column. Cross-
    entity group-bys (``customer__region``) need a join the Ibis demo path does
    not build; they're rejected with a clear message rather than silently
    dropped — the MetricFlow path remains the authority for those.
    """
    keys: dict[str, IbisExpr] = {}
    for token in group_by:
        base, _, grain = token.partition("__")
        if base in ("metric_time", time_dim):
            if time_dim is None:
                raise ConnectorError("model has no time dimension for a time group-by")
            col = table[time_dim]
            g = grain or "day"
            if g not in _GRAINS:
                raise ConnectorError(f"unknown time grain {g!r}; use one of {_GRAINS}")
            keys[token] = _truncate(col, g).name(token)
        elif (not grain and base in table.columns) or base in table.columns:
            keys[token] = table[base].name(token)
        else:
            raise ConnectorError(
                f"group-by {token!r} is not a local dimension on this metric's "
                "model; cross-entity group-bys are MetricFlow-only in this build"
            )
    return table, keys


def _truncate(col: IbisExpr, grain: str) -> IbisExpr:
    """Backend-portable timestamp truncation to a calendar grain."""
    unit = {"day": "D", "week": "W", "month": "M", "quarter": "Q", "year": "Y"}[grain]
    return col.truncate(unit)


def query_metric_ibis(
    metrics: list[str],
    group_by: list[str] | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    order_by: list[str] | None = None,
    limit: int | None = None,
    backend: IbisBackend | None = None,
) -> list[dict[str, str]]:
    """Answer a governed-metric query through Ibis on the configured backend.

    Same signature and same row shape as ``warehouse.query_metrics`` (the
    MetricFlow path) — that's the point: the MCP tool can route to either and
    the agent cannot tell which tier answered.

    ``backend`` is injectable for tests (a mock/local backend); production code
    passes ``None`` and gets the env-configured connection.
    """
    if not metrics:
        raise ConnectorError("at least one metric is required")

    manifest = warehouse.semantic_manifest()  # governed source of truth
    metric_defs = _metric_defs(manifest)
    measures = _measures_by_name(manifest)
    time_dims = _time_dim_by_relation(manifest)

    # All requested metrics must read from the same relation for the single-table
    # Ibis demo path (true for the simple/ratio metrics in this warehouse).
    relations: set[str] = set()
    for name in metrics:
        md = metric_defs.get(name)
        if md is None:
            raise ConnectorError(f"unknown metric {name!r}; call list_metrics first")
        for mname in md.measures:
            relations.add(measures[mname].relation)
    if len(relations) != 1:
        raise ConnectorError(
            "the Ibis path queries one mart at a time; requested metrics span "
            f"relations {sorted(relations)} — query them separately or use the "
            "MetricFlow path"
        )
    relation = relations.pop()
    time_dim = time_dims.get(relation)

    con = backend or make_backend()
    table = con.table(relation)

    # Time filter on the agg-time dimension.
    if (start_time or end_time) and time_dim is not None:
        col = table[time_dim]
        if start_time:
            table = table.filter(col >= start_time)
        if end_time:
            table = table.filter(col <= end_time)

    table, keys = _apply_group_by(table, group_by or [], time_dim)

    # Build the per-metric aggregate expressions (ratio = numerator / denom).
    aggs: dict[str, IbisExpr] = {}
    for name in metrics:
        md = metric_defs[name]
        parts = [_measure_column(table, measures[mn]) for mn in md.measures]
        if md.metric_type == "ratio":
            if len(parts) != 2:
                raise ConnectorError(f"ratio metric {name!r} needs exactly 2 measures")
            aggs[name] = (parts[0] / parts[1]).name(name)
        else:
            aggs[name] = parts[0].name(name)

    if keys:
        result = table.group_by(list(keys.values())).aggregate(**aggs)
    else:
        result = table.aggregate(**aggs)

    # Ordering: "-name" => descending; plain name => ascending.
    if order_by:
        sort_keys: list[IbisExpr] = []
        for token in order_by:
            desc = token.startswith("-")
            col = token[1:] if desc else token
            sort_keys.append(result[col].desc() if desc else result[col].asc())
        result = result.order_by(sort_keys)

    if limit is not None:
        result = result.limit(limit)

    rows = result.to_pyarrow().to_pylist()
    return [{k: _as_str(v) for k, v in row.items()} for row in rows]


def _as_str(value: Any) -> str:
    """Match the MetricFlow path: every cell comes back as a string."""
    if value is None:
        return ""
    return str(value)
