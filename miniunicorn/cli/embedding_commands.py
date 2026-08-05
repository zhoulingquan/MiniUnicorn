"""``miniunicorn embedding`` command group: setup, status, verify, rebuild.

These commands are thin adapters over the shared :class:`EmbeddingControl` so
the CLI, WebUI and AgentLoop always report the same status shape. ``status``
never exits non-zero for a not-ready model/index (it is a read-only snapshot);
the mutating commands exit ``1`` on operation failure. Argument errors are
surfaced by Typer as exit code ``2``. ``--json`` emits exactly one JSON object
on stdout; every diagnostic is written to stderr so machine consumers can parse
stdout cleanly.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import typer
from rich.table import Table

from miniunicorn.cli._terminal_render import console
from miniunicorn.config.paths import get_workspace_path
from miniunicorn.embedding.control import EmbeddingControl

embedding_app = typer.Typer(help="Manage local embedding and memory index")


def _control(workspace: str | None) -> EmbeddingControl:
    """Resolve the shared embedding control for a workspace."""
    return EmbeddingControl.for_workspace(get_workspace_path(workspace), configured=True)


def _render_status(payload: dict[str, Any]) -> None:
    """Render the four-section status as a Rich table with fixed Chinese labels."""
    table = Table(title="Embedding Memory 状态", show_header=False)
    table.add_column("项目", style="cyan", no_wrap=True)
    table.add_column("详情")

    table.add_row("模型", _format_model(payload.get("model") or {}))
    table.add_row("索引", _format_index(payload.get("index") or {}))
    table.add_row("来源同步", _format_sources(payload.get("sources") or {}))
    table.add_row("实际检索", _format_recall(payload.get("recall") or {}))

    console.print(table)


def _format_model(model: dict[str, Any]) -> str:
    parts = [str(model.get("state", ""))]
    if model.get("model_id"):
        parts.append(str(model["model_id"]))
    if model.get("dimension"):
        parts.append(f"{model['dimension']}d")
    if model.get("bytes"):
        parts.append(f"{model['bytes']}B")
    if model.get("last_error_code"):
        parts.append(f"err={model['last_error_code']}")
    return " ".join(p for p in parts if p)


def _format_index(index: dict[str, Any]) -> str:
    parts = [str(index.get("state", ""))]
    if index.get("bytes"):
        parts.append(f"{index['bytes']}B")
    if index.get("last_error_code"):
        parts.append(f"err={index['last_error_code']}")
    return " ".join(p for p in parts if p)


def _format_sources(sources: dict[str, Any]) -> str:
    indexed = sources.get("indexed", 0)
    discovered = sources.get("discovered", 0)
    return f"{indexed}/{discovered} indexed"


def _format_recall(recall: dict[str, Any]) -> str:
    if recall.get("active"):
        return "active"
    return str(recall.get("fallback_reason") or "inactive")


def _validate_index(control: EmbeddingControl) -> bool:
    """Validate the existing index database; never raise, never delete files."""
    if not control.db_path.is_file():
        return True
    from miniunicorn.agent.vector_index import VectorIndexManager

    index = VectorIndexManager(control.db_path)
    try:
        if not index.is_search_ready():
            return True
        validation = asyncio.run(index.validate(control.provider))
        return bool(validation.ok)
    except Exception:
        return False
    finally:
        index.close()


@embedding_app.command("status")
def status_command(
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    json_output: bool = typer.Option(False, "--json", help="Emit a single JSON object"),
) -> None:
    """Show embedding model, index, source sync and recall status."""
    payload = _control(workspace).status(configured=True).to_dict()
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        _render_status(payload)


@embedding_app.command("setup")
def setup_command(
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    force: bool = typer.Option(False, "--force", help="Re-download even if already ready"),
) -> None:
    """Download, hash-verify and self-test the pinned embedding model."""
    control = _control(workspace)
    asyncio.run(control.model_manager.setup(force=force))
    result = control.status(configured=True)
    _render_status(result.to_dict())
    if result.model.state != "ready":
        raise typer.Exit(1)


@embedding_app.command("verify")
def verify_command(
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
) -> None:
    """Run a real model self-test and validate the existing index."""
    control = _control(workspace)
    model_status = asyncio.run(control.model_manager.verify(run_self_test=True))
    index_ok = _validate_index(control)
    result = control.status(configured=True)
    _render_status(result.to_dict())
    if model_status.state != "ready" or not index_ok:
        raise typer.Exit(1)


@embedding_app.command("rebuild")
def rebuild_command(
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
) -> None:
    """Verify the model, then atomically rebuild the memory index."""
    from miniunicorn.agent.vector_index import VectorIndexManager

    control = _control(workspace)
    model_status = asyncio.run(control.model_manager.verify(run_self_test=True))
    if model_status.state != "ready":
        result = control.status(configured=True)
        _render_status(result.to_dict())
        raise typer.Exit(1)
    index = VectorIndexManager(control.db_path)
    try:
        report = asyncio.run(index.rebuild(control.catalog, control.provider))
    finally:
        index.close()
    result = control.status(configured=True)
    _render_status(result.to_dict())
    if report.state != "ready":
        raise typer.Exit(1)
