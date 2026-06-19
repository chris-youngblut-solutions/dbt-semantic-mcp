"""Ibis multi-backend connector — local DuckDB queries + backend-swap identity.

These tests are keyless and offline. The local path uses the dbt-built
``warehouse.duckdb``. The "hosted tier" is *simulated* with a second, in-memory
DuckDB connection seeded with the same mart rows — that is enough to prove the
interface is backend-agnostic (same call, same row shape, same numbers) without
touching a real network or any credential. The hosted-factory wiring
(postgres / snowflake / bigquery) is exercised with mocks so we confirm env
selection and that no secret is ever passed positionally.
"""

from __future__ import annotations

import os
from typing import Any
from unittest import mock

import ibis
import pytest

from dbt_semantic_mcp import connector, warehouse

# ---------------------------------------------------------------------------
# Local DuckDB path (the degrades-to-local fallback)
# ---------------------------------------------------------------------------


def test_default_backend_is_local_duckdb() -> None:
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DBT_SEMANTIC_MCP_BACKEND", None)
        assert connector.backend_name() == "duckdb"


def test_unsupported_backend_rejected() -> None:
    with (
        mock.patch.dict(os.environ, {"DBT_SEMANTIC_MCP_BACKEND": "redshift"}),
        pytest.raises(connector.ConnectorError),
    ):
        connector.backend_name()


def test_local_duckdb_simple_metric() -> None:
    rows = connector.query_metric_ibis(["revenue"])
    assert len(rows) == 1
    assert float(rows[0]["revenue"]) > 0


def test_local_duckdb_time_group_by_matches_metricflow() -> None:
    """The Ibis path and the MetricFlow path must agree on the governed number."""
    ib = {
        r["metric_time__year"][:4]: round(float(r["revenue"]), 2)
        for r in connector.query_metric_ibis(["revenue"], group_by=["metric_time__year"])
    }
    mf = {
        str(r["metric_time__year"])[:4]: round(float(r["revenue"]), 2)
        for r in warehouse.query_metrics(["revenue"], group_by=["metric_time__year"])
    }
    assert ib == mf
    assert set(ib) == {"2024", "2025"}


def test_local_duckdb_categorical_group_by_and_order() -> None:
    rows = connector.query_metric_ibis(
        ["order_count"], group_by=["status"], order_by=["-order_count"]
    )
    assert rows[0]["status"] == "completed"
    assert all("status" in r and "order_count" in r for r in rows)


def test_local_duckdb_ratio_metric() -> None:
    rows = connector.query_metric_ibis(["on_time_shipment_rate"])
    assert len(rows) == 1
    rate = float(rows[0]["on_time_shipment_rate"])
    assert 0.0 <= rate <= 1.0


def test_time_filter_and_limit() -> None:
    full = float(connector.query_metric_ibis(["revenue"])[0]["revenue"])
    y2025 = float(
        connector.query_metric_ibis(["revenue"], start_time="2025-01-01", end_time="2025-12-31")[0][
            "revenue"
        ]
    )
    assert 0 < y2025 < full
    capped = connector.query_metric_ibis(["revenue"], group_by=["metric_time__quarter"], limit=2)
    assert len(capped) == 2


def test_cross_mart_metric_rejected_clearly() -> None:
    # average_order_value mixes a measure from fct_order_items with one from
    # fct_orders; the single-table Ibis demo path rejects rather than guesses.
    with pytest.raises(connector.ConnectorError, match="one mart at a time"):
        connector.query_metric_ibis(["average_order_value"])


def test_unknown_metric_rejected() -> None:
    with pytest.raises(connector.ConnectorError, match="unknown metric"):
        connector.query_metric_ibis(["bogus"])


def test_cross_entity_group_by_rejected() -> None:
    # customer__region needs a join the Ibis demo path doesn't build.
    with pytest.raises(connector.ConnectorError, match="cross-entity"):
        connector.query_metric_ibis(["revenue"], group_by=["customer__region"])


# ---------------------------------------------------------------------------
# Backend-swap identity: same interface against a *different* backend instance
# ---------------------------------------------------------------------------


def _simulated_hosted_backend() -> Any:
    """A second in-memory DuckDB seeded with the marts, standing in for a
    hosted tier. Proves the query path is bound to the interface, not the
    physical local file: a different connection, same answers.
    """
    src = warehouse.warehouse_dir() / "target" / "warehouse.duckdb"
    local = ibis.duckdb.connect(str(src), read_only=True)
    hosted = ibis.duckdb.connect()  # fresh, in-memory
    for table in ("fct_order_items", "fct_orders", "dim_customers", "dim_products"):
        hosted.create_table(table, local.table(table).to_pyarrow())
    return hosted


def test_backend_swap_keeps_interface_identical() -> None:
    local_rows = connector.query_metric_ibis(
        ["revenue"], group_by=["metric_time__year"], order_by=["metric_time__year"]
    )
    hosted = _simulated_hosted_backend()
    hosted_rows = connector.query_metric_ibis(
        ["revenue"],
        group_by=["metric_time__year"],
        order_by=["metric_time__year"],
        backend=hosted,
    )
    # Identical keys (interface) and identical values (governed semantics).
    assert [r.keys() == hosted_rows[i].keys() for i, r in enumerate(local_rows)]
    assert [round(float(r["revenue"]), 2) for r in local_rows] == [
        round(float(r["revenue"]), 2) for r in hosted_rows
    ]


def test_backend_swap_ratio_metric_identical() -> None:
    local = connector.query_metric_ibis(["on_time_shipment_rate"])
    hosted = connector.query_metric_ibis(
        ["on_time_shipment_rate"], backend=_simulated_hosted_backend()
    )
    assert round(float(local[0]["on_time_shipment_rate"]), 6) == round(
        float(hosted[0]["on_time_shipment_rate"]), 6
    )


# ---------------------------------------------------------------------------
# Hosted-factory wiring (mocked — no real network, no creds in argv)
# ---------------------------------------------------------------------------


def test_postgres_factory_reads_env_not_args() -> None:
    env = {
        "DBT_SEMANTIC_MCP_BACKEND": "postgres",
        "PGHOST": "db.internal",
        "PGPORT": "5433",
        "PGDATABASE": "analytics",
        "PGUSER": "reader",
    }
    with mock.patch.dict(os.environ, env), mock.patch.object(connector, "_ibis_backend") as factory:
        connector.make_backend()
        factory.assert_called_once_with("postgres")
        pg = factory.return_value
        pg.connect.assert_called_once()
        kwargs = pg.connect.call_args.kwargs
        assert kwargs["host"] == "db.internal"
        assert kwargs["port"] == 5433
        assert kwargs["database"] == "analytics"
        # The password is never an argument — libpq/.pgpass supplies it.
        assert "password" not in kwargs


def test_snowflake_factory_reads_env_not_args() -> None:
    env = {
        "DBT_SEMANTIC_MCP_BACKEND": "snowflake",
        "SNOWFLAKE_ACCOUNT": "acme-xy12345",
        "SNOWFLAKE_USER": "svc_reader",
        "SNOWFLAKE_DATABASE": "ANALYTICS",
    }
    with mock.patch.dict(os.environ, env), mock.patch.object(connector, "_ibis_backend") as factory:
        connector.make_backend()
        factory.assert_called_once_with("snowflake")
        sf = factory.return_value
        sf.connect.assert_called_once()
        kwargs = sf.connect.call_args.kwargs
        assert kwargs["account"] == "acme-xy12345"
        assert kwargs["user"] == "svc_reader"
        assert kwargs["database"] == "ANALYTICS"


def test_bigquery_factory_reads_env_not_args() -> None:
    env = {
        "DBT_SEMANTIC_MCP_BACKEND": "bigquery",
        "BIGQUERY_PROJECT": "my-proj",
        "BIGQUERY_DATASET": "marts",
    }
    with mock.patch.dict(os.environ, env), mock.patch.object(connector, "_ibis_backend") as factory:
        connector.make_backend()
        factory.assert_called_once_with("bigquery")
        bq = factory.return_value
        bq.connect.assert_called_once()
        kwargs = bq.connect.call_args.kwargs
        assert kwargs["project_id"] == "my-proj"
        assert kwargs["dataset_id"] == "marts"


def test_missing_hosted_credentials_surface_clearly() -> None:
    # No SNOWFLAKE_ACCOUNT -> KeyError from os.environ[...] (no silent default,
    # no baked credential). We assert it raises rather than connecting.
    env = {"DBT_SEMANTIC_MCP_BACKEND": "snowflake"}
    with (
        mock.patch.dict(os.environ, env, clear=True),
        mock.patch.object(connector, "_ibis_backend") as factory,
    ):
        with pytest.raises(KeyError):
            connector.make_backend()
        factory.return_value.connect.assert_not_called()
