"""Architecture dependency-direction guards (pure AST).

Enforces the module-boundary rules from ``docs/architecture/module-boundaries.md``:

A. Business modules must not import ``miniunicorn.composition``.
   Entry points (``miniunicorn/cli/*``, ``miniunicorn/miniunicorn.py``) and the
   ``composition`` package itself are exempt.

B. ``miniunicorn.session`` and ``miniunicorn.channels`` must not import
   ``miniunicorn.agent``.  Dependency direction is one-way: the agent may
   depend on session/channels, never the reverse.

C. Modules must not access underscore-private attributes on names imported
   from a different top-level package.  Declared transitional exceptions are
   listed explicitly below and each exemption must be removed once the
   underlying coupling is resolved.

D. Sink packages (``providers`` / ``utils`` / ``security`` / ``config`` /
   ``bus`` / ``ledger``) must not import ``miniunicorn.agent``.  Dependency
   direction is one-way: the agent core may depend on its vocabulary and
   infrastructure leaves, never the reverse.  No exemptions.

The scanner is deliberately dependency-free (pure ``ast``): every
``miniunicorn/**/*.py`` file is parsed, import bindings are resolved, and
accesses are checked.  Instance-level private access on objects that are not
import-bound (e.g. ``loop._last_usage`` read by command handlers, ``state._*``
in tools) is not statically resolvable and is governed by the documented
exemption list in ``module-boundaries.md`` §4.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "miniunicorn"

# 业务模块：构成单向依赖的骨架。入口包(cli/webui/composition/根 facade)不受
# “不得 import composition” 限制。
BUSINESS_PACKAGES = frozenset(
    {
        "bus",
        "command",
        "agent",
        "session",
        "channels",
        "providers",
        "security",
        "utils",
        "cron",
        "config",
        "memory",
    }
)

# Rule B 已声明的过渡期例外：channels 对 agent 的既有功能依赖。
# - channel.py 经 agent.tools.mcp.request_mcp_reload 触发 MCP 服务热重载。
# - handlers/skills.py 复用 agent.skills 的 SkillsLoader 校验/加载技能。
# - handlers/agents.py 复用 agent.routes_agents 的子代理 CRUD/生成 handler
#   (create_agent 工具与 /api/agents* 端点共用同一实现)。
# 长期应改为依赖注入(bus 事件或注入服务),届时移除对应豁免条目。
AGENT_IMPORT_EXEMPTIONS = frozenset(
    {
        ("channels/websocket/handlers/agents", "miniunicorn.agent.routes_agents"),
        ("channels/websocket/handlers/skills", "miniunicorn.agent.skills"),
    }
)

# Rule C 已声明的过渡期例外：跨包下划线私有属性访问。
# - composition/gateway.py:99 对 miniunicorn.cli.commands._migrate_cron_store 的
#   后期绑定(组合层复用 cli.commands 的迁移工具),见 module-boundaries.md §4。
PRIVATE_ATTR_EXEMPTIONS = frozenset(
    {
        ("composition/gateway", "commands._migrate_cron_store"),
    }
)


def _iter_source_files() -> Iterator[Path]:
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _rel_module(path: Path) -> str:
    return path.relative_to(SRC_ROOT).as_posix().removesuffix(".py")


def _package_of(rel_module: str) -> str:
    return rel_module.split("/")[0]


def _import_targets(tree: ast.AST) -> list[tuple[int, str]]:
    """Collect ``(lineno, dotted_target)`` for every import statement."""
    targets: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            targets.append((node.lineno, node.module))
    return targets


def _bindings(tree: ast.AST) -> dict[str, str]:
    """Resolve every top-level imported name to its dotted target."""
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bindings[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name != "*":
                    bindings[alias.asname or alias.name] = node.module
    return bindings


def _attribute_root(obj: ast.AST) -> str | None:
    """Return the base ``ast.Name`` of an attribute chain, if any."""
    if isinstance(obj, ast.Name):
        return obj.id
    if isinstance(obj, ast.Attribute):
        return _attribute_root(obj.value)
    return None


def test_business_modules_do_not_import_composition() -> None:
    violations: list[str] = []
    for path in _iter_source_files():
        rel = _rel_module(path)
        if _package_of(rel) not in BUSINESS_PACKAGES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for lineno, target in _import_targets(tree):
            if target.split(".")[0] == "composition":
                violations.append(f"{rel}:{lineno} imports {target}")
    assert not violations, "business modules must not import composition:\n" + "\n".join(violations)


def test_session_and_channels_do_not_import_agent() -> None:
    found: set[tuple[str, str]] = set()
    for path in _iter_source_files():
        rel = _rel_module(path)
        if _package_of(rel) not in {"session", "channels"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for _, target in _import_targets(tree):
            if target == "miniunicorn.agent" or target.startswith("miniunicorn.agent."):
                found.add((rel, target))
    assert found == AGENT_IMPORT_EXEMPTIONS, (
        "session/channels must not import agent; mismatch between current "
        "imports and the declared exemptions:\n"
        f"  unexpected: {sorted(found - AGENT_IMPORT_EXEMPTIONS)}\n"
        f"  stale:      {sorted(AGENT_IMPORT_EXEMPTIONS - found)}"
    )


def test_sink_packages_do_not_import_agent() -> None:
    found: set[tuple[str, str]] = set()
    for path in _iter_source_files():
        rel = _rel_module(path)
        if _package_of(rel) not in {
            "providers",
            "utils",
            "security",
            "config",
            "bus",
            "ledger",
            "memory",
        }:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for _, target in _import_targets(tree):
            if target == "miniunicorn.agent" or target.startswith("miniunicorn.agent."):
                found.add((rel, target))
    assert found == set(), (
        "providers/utils/security/config/bus/ledger/memory must not import agent; "
        f"violations:\n  {sorted(found)}"
    )


def test_no_cross_package_underscore_private_access() -> None:
    found: set[tuple[str, str]] = set()
    for path in _iter_source_files():
        rel = _rel_module(path)
        package = _package_of(rel)
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        bindings = _bindings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            attr = node.attr
            if not attr.startswith("_") or attr.startswith("__"):
                continue
            root = _attribute_root(node.value)
            if root is None or root not in bindings:
                continue
            target = bindings[root]
            if not target.startswith("miniunicorn."):
                continue
            target_package = target.split(".")[1]
            if target_package != package:
                found.add((rel, f"{root}.{attr}"))
    assert found == PRIVATE_ATTR_EXEMPTIONS, (
        "cross-package underscore-private attribute access must be declared:\n"
        f"  unexpected: {sorted(found - PRIVATE_ATTR_EXEMPTIONS)}\n"
        f"  stale:      {sorted(PRIVATE_ATTR_EXEMPTIONS - found)}"
    )
