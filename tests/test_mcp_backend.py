"""End-to-end: the Ibis backend tool answers over the real MCP stdio surface.

Same transport as test_mcp_server.py, but exercising ``query_metric_via_backend``
(the connector seam) and ``active_backend``. Offline: the server defaults to the
local DuckDB backend, so no network or credential is involved.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parents[1]


async def _roundtrip() -> tuple[set[str], str, list[dict[str, Any]]]:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "dbt_semantic_mcp.server"],
        cwd=str(REPO_ROOT),
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()

        backend_res = await session.call_tool("active_backend", {})
        assert not backend_res.isError
        backend_content = backend_res.content[0]
        assert backend_content.type == "text"
        backend = json.loads(backend_content.text)["backend"]

        result = await session.call_tool(
            "query_metric_via_backend",
            {"metrics": ["revenue"], "group_by": ["metric_time__year"]},
        )
        assert not result.isError
        rows: list[dict[str, Any]] = []
        for content in result.content:
            assert content.type == "text"
            parsed: Any = json.loads(content.text)
            if isinstance(parsed, list):
                rows.extend(dict(r) for r in parsed)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            else:
                rows.append(parsed)
        return {t.name for t in tools.tools}, backend, rows


def test_backend_tool_roundtrip_returns_revenue_rows() -> None:
    names, backend, rows = asyncio.run(asyncio.wait_for(_roundtrip(), timeout=120))
    # The new tools sit beside the original three — nothing removed.
    assert {"list_metrics", "query_metric", "describe_lineage"} <= names
    assert {"query_metric_via_backend", "active_backend"} <= names
    assert backend == "duckdb"  # offline default
    assert len(rows) == 2  # 2024 and 2025
    assert all(float(r["revenue"]) > 0 for r in rows)
