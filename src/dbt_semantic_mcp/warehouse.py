"""Read side of the MCP server: metric catalog, MetricFlow queries, dbt lineage.

Everything here goes through the governed artifacts — the semantic manifest for
the metric catalog, the `mf` CLI for queries, the dbt manifest for lineage.
No SQL is composed from user input.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

MF_TIMEOUT_SECONDS = 120


class WarehouseError(RuntimeError):
    """A warehouse artifact is missing or a MetricFlow call failed."""


def warehouse_dir() -> Path:
    """The dbt project directory; override with DBT_SEMANTIC_MCP_WAREHOUSE."""
    env = os.environ.get("DBT_SEMANTIC_MCP_WAREHOUSE")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[2] / "warehouse"


def _load_json(path: Path, hint: str) -> dict[str, Any]:
    if not path.exists():
        raise WarehouseError(f"{path} missing — run `{hint}` in {path.parents[1]}")
    return json.loads(path.read_text())


def _semantic_manifest(wh: Path) -> dict[str, Any]:
    return _load_json(wh / "target" / "semantic_manifest.json", "dbt parse")


def semantic_manifest() -> dict[str, Any]:
    """The governed semantic manifest for the active warehouse.

    Public accessor so other query paths (e.g. the Ibis connector) can read the
    same governed metric/measure definitions the MetricFlow path reads, instead
    of composing SQL from input.
    """
    return _semantic_manifest(warehouse_dir())


def _dbt_manifest(wh: Path) -> dict[str, Any]:
    return _load_json(wh / "target" / "manifest.json", "dbt build")


def _group_bys_for_measures(manifest: dict[str, Any], measure_names: set[str]) -> list[str]:
    """Approximate the queryable group-bys for a set of measures.

    Local dimensions are prefixed with the owning model's primary entity; a
    foreign entity pulls in the dimensions of the model where that entity is
    primary (one hop). `mf query` remains the authority — on a bad group-by it
    returns the valid candidates in its error message.
    """
    models: list[dict[str, Any]] = manifest["semantic_models"]
    primary_dims: dict[str, list[str]] = {}
    for sm in models:
        for ent in sm["entities"]:
            if ent["type"] == "primary":
                primary_dims[ent["name"]] = [d["name"] for d in sm["dimensions"]]

    group_bys: set[str] = set()
    for sm in models:
        if not measure_names.intersection(m["name"] for m in sm["measures"]):
            continue
        group_bys.add("metric_time")
        primary = next(e["name"] for e in sm["entities"] if e["type"] == "primary")
        group_bys.update(f"{primary}__{d['name']}" for d in sm["dimensions"])
        for ent in sm["entities"]:
            if ent["type"] == "foreign":
                group_bys.update(f"{ent['name']}__{d}" for d in primary_dims.get(ent["name"], []))
    return sorted(group_bys)


def list_metrics() -> list[dict[str, Any]]:
    """The governed metric catalog from the semantic manifest."""
    manifest = _semantic_manifest(warehouse_dir())
    catalog: list[dict[str, Any]] = []
    for metric in manifest["metrics"]:
        measures = {m["name"] for m in metric["type_params"]["input_measures"]}
        catalog.append(
            {
                "name": metric["name"],
                "label": metric["label"],
                "type": metric["type"],
                "description": (metric["description"] or "").strip(),
                "group_bys": _group_bys_for_measures(manifest, measures),
            }
        )
    return catalog


def query_metrics(
    metrics: list[str],
    group_by: list[str] | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    order_by: list[str] | None = None,
    limit: int | None = None,
) -> list[dict[str, str]]:
    """Run `mf query` against the governed semantic layer; return CSV rows."""
    mf = shutil.which("mf")
    if mf is None:
        raise WarehouseError("`mf` CLI not on PATH — run via `uv run`")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "result.csv"
        cmd = [mf, "query", "--quiet", "--metrics", ",".join(metrics), "--csv", str(out)]
        if group_by:
            cmd += ["--group-by", ",".join(group_by)]
        if start_time:
            cmd += ["--start-time", start_time]
        if end_time:
            cmd += ["--end-time", end_time]
        if order_by:
            cmd += ["--order", ",".join(order_by)]
        if limit is not None:
            cmd += ["--limit", str(limit)]
        proc = subprocess.run(
            cmd,
            cwd=warehouse_dir(),
            capture_output=True,
            text=True,
            timeout=MF_TIMEOUT_SECONDS,
            check=False,
        )
        if proc.returncode != 0 or not out.exists():
            raise WarehouseError(
                f"mf query failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
            )
        with out.open(newline="") as f:
            return list(csv.DictReader(f))


def describe_lineage(node_name: str) -> dict[str, Any]:
    """Upstream and downstream dbt nodes for a model, seed, or metric name.

    A metric name resolves to the mart model(s) its measures read from, then
    reports that model's lineage.
    """
    wh = warehouse_dir()
    semantic = _semantic_manifest(wh)
    manifest = _dbt_manifest(wh)

    resolved = node_name
    metric = next((m for m in semantic["metrics"] if m["name"] == node_name), None)
    if metric is not None:
        measures = {m["name"] for m in metric["type_params"]["input_measures"]}
        for sm in semantic["semantic_models"]:
            if measures.intersection(m["name"] for m in sm["measures"]):
                resolved = sm["node_relation"]["alias"]
                break

    nodes: dict[str, Any] = manifest["nodes"] | manifest.get("sources", {})
    unique_id = next(
        (uid for uid, node in nodes.items() if node.get("name") == resolved),
        None,
    )
    if unique_id is None:
        known = sorted(n.get("name", "?") for n in manifest["nodes"].values())
        raise WarehouseError(f"no dbt node named '{resolved}'; known nodes: {known}")

    def names(ids: list[str]) -> list[str]:
        return sorted(uid.rsplit(".", 1)[-1] for uid in ids)

    return {
        "requested": node_name,
        "resolved_node": resolved,
        "unique_id": unique_id,
        "upstream": names(manifest["parent_map"].get(unique_id, [])),
        "downstream": names(manifest["child_map"].get(unique_id, [])),
    }
