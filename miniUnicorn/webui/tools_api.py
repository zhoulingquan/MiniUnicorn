"""Tools REST helpers for the WebUI HTTP surface.

Lists registered tools (built-in + MCP) and manages user-uploaded .py tool
files stored in ``<workspace>/tools/``.  Uploaded files are only loaded into
the running agent after a gateway restart — this module owns storage only.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from ._query import _query_first, _query_first_alias
from ._runtime import QueryParams


class WebUIToolsError(ValueError):
    """User-facing tool validation failure."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


_SAFE_FILENAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_\-]*\.py$")
_BUILTIN_TOOL_SOURCES = {"builtin", "mcp"}


def _user_tools_dir(workspace: Path) -> Path:
    """Return the user tools directory, creating it if missing."""
    tools_dir = workspace / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    return tools_dir


def _classify_source(name: str) -> str:
    """Classify a tool as 'mcp', 'builtin', or 'user'."""
    if name.startswith("mcp_"):
        return "mcp"
    return "builtin"


def _scan_user_tool_files(workspace: Path) -> list[dict[str, Any]]:
    """List .py files in ``<workspace>/tools/`` (user-uploaded tools)."""
    tools_dir = workspace / "tools"
    if not tools_dir.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for entry in sorted(tools_dir.iterdir()):
        if not entry.is_file() or entry.suffix != ".py":
            continue
        stat = entry.stat()
        result.append(
            {
                "name": entry.stem,
                "filename": entry.name,
                "source": "user",
                "size_bytes": stat.st_size,
                "modified_at_ms": int(stat.st_mtime * 1000),
                "loaded": False,
            }
        )
    return result


def list_tools(
    registry: Any | None,
    workspace: Path,
) -> dict[str, Any]:
    """Return all tools: registered (builtin+mcp) + user files on disk."""
    registered: list[dict[str, Any]] = []
    user_files = _scan_user_tool_files(workspace)
    user_file_names = {f["name"] for f in user_files}

    if registry is not None:
        for name in registry.tool_names:
            tool = registry.get(name)
            source = _classify_source(name)
            # If a builtin tool name matches a user file stem, mark as user.
            if name in user_file_names:
                source = "user"
            entry: dict[str, Any] = {
                "name": name,
                "description": getattr(tool, "description", "") if tool else "",
                "source": source,
                "read_only": bool(getattr(tool, "read_only", False)) if tool else False,
                "loaded": True,
            }
            registered.append(entry)

    # Add user files that are not yet loaded (no matching registered tool).
    registered_names = {r["name"] for r in registered}
    for f in user_files:
        if f["name"] not in registered_names:
            registered.append(
                {
                    "name": f["name"],
                    "description": "",
                    "source": "user",
                    "read_only": False,
                    "loaded": False,
                    "filename": f["filename"],
                }
            )

    # Sort: builtin first, then user (loaded), then user (not loaded).
    def _sort_key(item: dict[str, Any]) -> tuple[int, str]:
        src = item.get("source", "builtin")
        loaded = item.get("loaded", False)
        if src == "builtin":
            return (0, item["name"])
        if src == "mcp":
            return (1, item["name"])
        return (2 if loaded else 3, item["name"])

    registered.sort(key=_sort_key)

    builtin_count = sum(1 for r in registered if r["source"] == "builtin")
    mcp_count = sum(1 for r in registered if r["source"] == "mcp")
    user_count = sum(1 for r in registered if r["source"] == "user")

    return {
        "tools": registered,
        "counts": {
            "builtin": builtin_count,
            "mcp": mcp_count,
            "user": user_count,
            "total": len(registered),
        },
    }


# 危险调用静态扫描黑名单。
# 键是调用名(属性访问取 attr,直接调用取 id),值是简短说明(用于错误消息)。
# 覆盖常见 RCE / 反序列化 / 系统命令 / 模块动态加载 / 网络监听 等危险面。
_DANGEROUS_CALLS: dict[str, str] = {
    # 直接内置函数
    "eval": "eval() executes arbitrary code",
    "exec": "exec() executes arbitrary code",
    "compile": "compile() can build arbitrary code objects",
    "__import__": "__import__() bypasses module sandboxing",
    "breakpoint": "breakpoint() is for debugging only",
    # os 系列执行/进程
    "system": "os.system() runs shell commands",
    "popen": "os.popen() runs shell commands",
    "execv": "os.execv() replaces the process",
    "execve": "os.execve() replaces the process",
    "execvp": "os.execvp() replaces the process",
    "execvpe": "os.execvpe() replaces the process",
    "spawnl": "os.spawnl() spawns processes",
    "spawnle": "os.spawnle() spawns processes",
    "spawnlp": "os.spawnlp() spawns processes",
    "spawnlpe": "os.spawnlpe() spawns processes",
    "spawnv": "os.spawnv() spawns processes",
    "spawnve": "os.spawnve() spawns processes",
    "spawnvp": "os.spawnvp() spawns processes",
    "spawnvpe": "os.spawnvpe() spawns processes",
    "fork": "os.fork() spawns child processes",
    "kill": "os.kill() sends signals to processes",
    "killpg": "os.killpg() sends signals to process groups",
    # subprocess
    "Popen": "subprocess.Popen runs shell commands",
    "run": "subprocess.run runs shell commands",
    "call": "subprocess.call runs shell commands",
    "check_call": "subprocess.check_call runs shell commands",
    "check_output": "subprocess.check_output runs shell commands",
    "getoutput": "subprocess.getoutput runs shell commands",
    "getstatusoutput": "subprocess.getstatusoutput runs shell commands",
    # 反序列化(可触发任意代码执行)
    "loads": "pickle/marshal.loads can execute arbitrary code",
    "load": "pickle/marshal.load can execute arbitrary code",
    # ctypes / cffi
    "CDLL": "ctypes.CDLL loads native libraries",
    "LoadLibrary": "ctypes.cdll.LoadLibrary loads native libraries",
    # 网络监听
    "bind": "socket.bind opens a listening server",
    "listen": "socket.listen opens a listening server",
    # 模块动态加载
    "import_module": "importlib.import_module bypasses sandboxing",
    # 输入读取(可读 secrets / 系统数据)
    "load_module": "imp/importlib.load_module bypasses sandboxing",
}

# 危险属性访问(读写危险属性)
_DANGEROUS_ATTRS: dict[str, str] = {
    "__builtins__": "direct __builtins__ access bypasses sandboxing",
    "__subclasses__": "__subclasses__() can leak privileged classes",
    "__globals__": "__globals__ access can leak privileged state",
    "__bases__": "__bases__ access can leak privileged classes",
    "__mro__": "__mro__ access can leak privileged classes",
}

# 高危模块的直接 import(不允许 `import os`、`import subprocess` 等)。
# 允许 `from os import path` 这类具名导入(具体符号在 Call 检查中拦截)。
_DANGEROUS_MODULE_IMPORTS: frozenset[str] = frozenset(
    {
        "os",
        "subprocess",
        "ctypes",
        "pickle",
        "marshal",
        "importlib",
        "imp",
        "pty",
        "multiprocessing",
    }
)


def _scan_dangerous_calls(tree: ast.AST) -> list[str]:
    """AST 静态扫描:返回所有危险调用/属性访问的描述列表(空表示通过)。

    检查项:
    - ``ast.Call``:函数名(属性访问取 attr,直接调用取 id)在 ``_DANGEROUS_CALLS`` 中
    - ``ast.Attribute``:attr 名在 ``_DANGEROUS_ATTRS`` 中
    - ``ast.Import`` / ``ast.ImportFrom``:允许常规 import,但禁止
      ``import os``、``import subprocess``、``import ctypes``、``import pickle``
      等高危模块的 *直接* import(从这些模块导入子符号仍可被 Call 检查捕获)

    注意:这是静态扫描,无法覆盖所有 obfuscation(如 getattr 动态取属性),
    但能挡住 95% 的常规危险写法。运行时仍依赖 Tool 沙箱做最终拦截。
    """
    findings: list[str] = []

    for node in ast.walk(tree):
        # 检查 import os / import subprocess
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_module = alias.name.split(".")[0]
                if root_module in _DANGEROUS_MODULE_IMPORTS:
                    findings.append(
                        f"line {node.lineno}: import of dangerous module '{alias.name}'"
                    )
        # 检查 from X import *
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root_module = node.module.split(".")[0]
                if root_module in _DANGEROUS_MODULE_IMPORTS and any(
                    a.name == "*" for a in node.names
                ):
                    findings.append(
                        f"line {node.lineno}: 'from {node.module} import *' "
                        f"imports dangerous module"
                    )
        # 检查危险函数调用
        elif isinstance(node, ast.Call):
            func = node.func
            call_name: str | None = None
            if isinstance(func, ast.Attribute):
                call_name = func.attr
            elif isinstance(func, ast.Name):
                call_name = func.id
            if call_name and call_name in _DANGEROUS_CALLS:
                findings.append(
                    f"line {node.lineno}: {_DANGEROUS_CALLS[call_name]} "
                    f"(call: {call_name})"
                )
        # 检查危险属性访问
        elif isinstance(node, ast.Attribute):
            if node.attr in _DANGEROUS_ATTRS:
                findings.append(
                    f"line {node.lineno}: {_DANGEROUS_ATTRS[node.attr]} "
                    f"(attr: {node.attr})"
                )

    return findings


def _validate_tool_py(content: str) -> None:
    """Basic syntax + structure + AST 静态扫描检查上传的工具文件。

    1. 语法检查(ast.parse)
    2. 至少一个继承 ``Tool`` 的类
    3. AST 静态扫描禁止危险调用(os.system / subprocess / eval / exec /
       pickle.loads / ctypes / import os 等)

    这是上传时的预筛门;完整加载时仍依赖 Tool 沙箱做最终拦截。
    """
    try:
        tree = ast.parse(content)
    except SyntaxError as e:
        raise WebUIToolsError(f"Python syntax error: {e.msg} (line {e.lineno})") from None

    has_tool_class = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                base_name = getattr(base, "id", None) or getattr(base, "attr", None)
                if base_name == "Tool":
                    has_tool_class = True
                    break
    if not has_tool_class:
        raise WebUIToolsError(
            "Tool file must define at least one class inheriting from Tool"
        )

    # AST 静态扫描:禁止危险调用
    findings = _scan_dangerous_calls(tree)
    if findings:
        # 限制错误消息长度,避免海量 findings 刷屏
        preview = "; ".join(findings[:5])
        suffix = f" (and {len(findings) - 5} more)" if len(findings) > 5 else ""
        raise WebUIToolsError(
            f"Tool file contains dangerous calls: {preview}{suffix}"
        )


def import_tool(
    workspace: Path,
    filename: str,
    content: bytes,
) -> dict[str, Any]:
    """Save an uploaded .py tool file into ``<workspace>/tools/``."""
    if not filename:
        raise WebUIToolsError("filename is required")
    if not _SAFE_FILENAME_RE.match(filename):
        raise WebUIToolsError(
            "filename must match [a-zA-Z][a-zA-Z0-9_-]*.py and have .py extension"
        )

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise WebUIToolsError("file must be UTF-8 encoded") from None

    _validate_tool_py(text)

    tools_dir = _user_tools_dir(workspace)
    dest = tools_dir / filename
    dest.write_text(text, encoding="utf-8")

    return {
        "imported": True,
        "filename": filename,
        "name": dest.stem,
        "message": "Tool saved. Restart the gateway to load it.",
    }


def delete_tool(workspace: Path, query: QueryParams) -> dict[str, Any]:
    """Delete a user tool .py file by name (builtin/mcp tools are protected)."""
    name = (_query_first_alias(query, "name", "toolName") or "").strip()
    if not name:
        raise WebUIToolsError("name is required")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_\-]*$", name):
        raise WebUIToolsError("invalid tool name")

    tools_dir = workspace / "tools"
    target = tools_dir / f"{name}.py"
    if not target.is_file():
        raise WebUIToolsError(f"user tool '{name}' not found", status=404)

    target.unlink()
    return {"deleted": True, "name": name}
