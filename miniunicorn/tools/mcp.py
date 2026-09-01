"""MCP client: connects to MCP servers and wraps their tools as native MiniUnicorn tools."""

import asyncio
import os
import re
import shutil
import urllib.parse
from contextlib import AsyncExitStack, suppress
from typing import Any, Awaitable, Callable, Mapping, Protocol
from weakref import WeakKeyDictionary

import httpx
from loguru import logger

from miniunicorn.bus.events import (
    INBOUND_META_RUNTIME_CONTROL,
    RUNTIME_CONTROL_ACK,
    RUNTIME_CONTROL_MCP_RELOAD,
    InboundMessage,
)
from miniunicorn.tools.base import Tool
from miniunicorn.tools.registry import ToolRegistry

# Transient connection errors that warrant a single retry.
# These typically happen when an MCP server restarts or a network
# connection is interrupted between calls.
_TRANSIENT_EXC_NAMES: frozenset[str] = frozenset(
    (
        "ClosedResourceError",
        "BrokenResourceError",
        "EndOfStream",
        "BrokenPipeError",
        "ConnectionResetError",
        "ConnectionRefusedError",
        "ConnectionAbortedError",
        "ConnectionError",
    )
)

_WINDOWS_SHELL_LAUNCHERS: frozenset[str] = frozenset(("npx", "npm", "pnpm", "yarn", "bunx"))

# Characters allowed in tool names by model providers (Anthropic, OpenAI, etc.).
# Replace anything outside [a-zA-Z0-9_-] with underscore and collapse runs.
_SANITIZE_RE = re.compile(r"_+")


def _sanitize_name(name: str) -> str:
    """Sanitize an MCP-derived name for model API compatibility."""
    return _SANITIZE_RE.sub("_", re.sub(r"[^a-zA-Z0-9_-]", "_", name))


def _is_transient(exc: BaseException) -> bool:
    """Check if an exception looks like a transient connection error."""
    return type(exc).__name__ in _TRANSIENT_EXC_NAMES


# Type aliases for the retry helper below.
_McpInvoker = Callable[[], Awaitable[Any]]
_McpResultExtractor = Callable[[Any], str]
_McpSpecializedErrorHandler = Callable[[BaseException, str, str], str | None]


class RuntimeState(Protocol):
    """AgentLoop 暴露给 MCP 生命周期管理的运行时状态表面。

    AgentLoop 按结构化子类型满足此协议。此前这些入口的 ``state`` 参数标为
    ``Any``, 属性访问完全绕过类型检查; 收窄为协议后拼写错误/类型不匹配
    可被静态检查捕获。
    """

    _mcp_servers: dict[str, Any]
    _mcp_stacks: dict[str, AsyncExitStack]
    _mcp_connecting: bool
    _mcp_connected: bool

    def _set_mcp_servers(self, servers: dict[str, Any]) -> None:
        """Replace the configured MCP server table (hot-reload write-back)."""
        ...


# reload 互斥锁按 runtime state 实例(通常是 AgentLoop)弱引用持有。
_RELOAD_LOCKS: WeakKeyDictionary[RuntimeState, asyncio.Lock] = WeakKeyDictionary()


async def _call_mcp_with_retry(
    *,
    name: str,
    kind: str,
    timeout: float,
    invoke: _McpInvoker,
    extract_text: _McpResultExtractor,
    specialized_error_handler: _McpSpecializedErrorHandler | None = None,
) -> str:
    """Run an MCP call with a single retry on transient errors.

    Centralizes the retry / timeout / cancel / error-handling pattern that
    was previously duplicated across ``MCPToolWrapper.execute``,
    ``MCPResourceWrapper.execute`` and ``MCPPromptWrapper.execute``.

    Args:
        name: Wrapper name (sanitized), used in log messages and error strings.
        kind: Short human label for the call type, e.g. ``"tool call"``,
            ``"resource read"``, ``"prompt call"``. Used to build user-facing
            error strings like ``(MCP tool call failed: ...)``.
        timeout: Per-call timeout in seconds.
        invoke: Zero-arg coroutine factory that performs the actual MCP call.
        extract_text: Converts a successful raw result into the final string.
        specialized_error_handler: Optional hook for exception types that need
            bespoke handling (e.g. ``McpError`` carries a structured
            ``error.code`` / ``error.message``). Returning a string from this
            handler short-circuits the generic error path; returning ``None``
            lets the call fall through to the transient-retry / generic-error
            logic.

    Returns:
        The extracted text on success, or a parenthesized error string on
        failure (matching the prior inline implementation byte-for-byte).
    """
    for attempt in range(2):  # At most 1 retry
        try:
            result = await asyncio.wait_for(invoke(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("MCP {} '{}' timed out after {}s", kind, name, timeout)
            return f"(MCP {kind} timed out after {timeout}s)"
        except asyncio.CancelledError:
            # MCP SDK's anyio cancel scopes can leak CancelledError on timeout/failure.
            # Re-raise only if our task was externally cancelled (e.g. /stop).
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                raise
            logger.warning("MCP {} '{}' was cancelled by server/SDK", kind, name)
            return f"(MCP {kind} was cancelled)"
        except Exception as exc:
            if specialized_error_handler is not None:
                specialized_result = specialized_error_handler(exc, name, kind)
                if specialized_result is not None:
                    return specialized_result
            if _is_transient(exc):
                if attempt == 0:
                    logger.warning(
                        "MCP {} '{}' hit transient error ({}), retrying once...",
                        kind,
                        name,
                        type(exc).__name__,
                    )
                    await asyncio.sleep(1)  # Brief backoff before retry
                    continue
                # Second transient failure — give up with retry-specific message
                logger.exception(
                    "MCP {} '{}' failed after retry: {}",
                    kind,
                    name,
                    type(exc).__name__,
                )
                return f"(MCP {kind} failed after retry: {type(exc).__name__})"
            logger.exception(
                "MCP {} '{}' failed: {}: {}",
                kind,
                name,
                type(exc).__name__,
                exc,
            )
            return f"(MCP {kind} failed: {type(exc).__name__})"
        else:
            # Success — extract result via the caller-supplied converter.
            return extract_text(result)

    return f"(MCP {kind} failed)"  # Unreachable, but satisfies type checkers


def _redact_url_for_log(url: str) -> str:
    """Strip credentials from a URL before writing it to logs.

    MCP URLs frequently embed credentials in userinfo (``https://user:pass@host``)
    or query parameters (``https://host/mcp?key=...``). Keep scheme/host/path
    and query key names; hide values and drop the fragment entirely.
    """
    try:
        p = urllib.parse.urlparse(url)
        netloc = p.hostname or ""
        if p.port:
            netloc = f"{netloc}:{p.port}"
        query = "&".join(
            f"{key}=***" if value else key
            for key, value in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        )
        return urllib.parse.urlunparse((p.scheme, netloc, p.path, p.params, query, ""))
    except Exception:
        return "<invalid-url>"


def _validate_mcp_url(url: str) -> str:
    """Pre-flight validation for an MCP HTTP(S) server URL. Returns error or ''.

    Blocks cloud metadata endpoints (169.254.0.0/16 etc.) which are never
    legitimate MCP targets, even when a hostname resolves to them. Loopback
    and RFC1918 stay allowed — local MCP servers are a common deployment.
    """
    import ipaddress
    import socket

    from miniunicorn.security.network import _is_metadata_blocked

    try:
        p = urllib.parse.urlparse(url)
    except ValueError as e:
        return f"invalid URL: {e}"
    if p.scheme not in ("http", "https"):
        return f"unsupported scheme '{p.scheme or 'none'}' (only http/https)"
    hostname = p.hostname
    if not hostname:
        return "missing hostname"
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return f"cannot resolve hostname: {hostname}"
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _is_metadata_blocked(addr):
            return f"host {hostname} resolves to blocked address {addr}"
    return ""


async def _probe_http_url(url: str, timeout: float = 3.0) -> bool:
    """Quick TCP probe to check if an HTTP MCP server is reachable.

    Avoids entering ``streamable_http_client`` / ``sse_client`` when the port is
    closed — those transports use anyio task groups whose cleanup can raise
    ``RuntimeError`` / ``ExceptionGroup`` that escape the caller's try/except
    and crash the event loop.

    Fail-closed on unparsable URLs: a missing hostname returns False instead
    of falling back to 127.0.0.1.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname
    port = parsed.port
    if not port:
        port = 443 if parsed.scheme == "https" else 80
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


def _windows_command_basename(command: str) -> str:
    """Return the lowercase basename for a Windows command or path."""
    return command.replace("\\", "/").rsplit("/", maxsplit=1)[-1].lower()


def _normalize_windows_stdio_command(
    command: str,
    args: list[str] | None,
    env: dict[str, str] | None,
) -> tuple[str, list[str], dict[str, str] | None]:
    """Wrap Windows shell launchers so MCP stdio servers start reliably."""
    normalized_args = list(args or [])
    if os.name != "nt":
        return command, normalized_args, env

    basename = _windows_command_basename(command)
    if basename in {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return command, normalized_args, env

    if basename.endswith((".exe", ".com")):
        return command, normalized_args, env

    resolved = shutil.which(command, path=(env or {}).get("PATH")) or command
    resolved_basename = _windows_command_basename(resolved)
    should_wrap = (
        basename in _WINDOWS_SHELL_LAUNCHERS
        or basename.endswith((".cmd", ".bat"))
        or resolved_basename.endswith((".cmd", ".bat"))
    )
    if not should_wrap:
        return command, normalized_args, env

    comspec = (env or {}).get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"
    return comspec, ["/d", "/c", command, *normalized_args], env


def _extract_nullable_branch(options: Any) -> tuple[dict[str, Any], bool] | None:
    """Return the single non-null branch for nullable unions."""
    if not isinstance(options, list):
        return None

    non_null: list[dict[str, Any]] = []
    saw_null = False
    for option in options:
        if not isinstance(option, dict):
            return None
        if option.get("type") == "null":
            saw_null = True
            continue
        non_null.append(option)

    if saw_null and len(non_null) == 1:
        return non_null[0], True
    return None


def _normalize_schema_for_openai(schema: Any) -> dict[str, Any]:
    """Normalize only nullable JSON Schema patterns for tool definitions."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    normalized = dict(schema)

    raw_type = normalized.get("type")
    if isinstance(raw_type, list):
        non_null = [item for item in raw_type if item != "null"]
        if "null" in raw_type and len(non_null) == 1:
            normalized["type"] = non_null[0]
            normalized["nullable"] = True

    for key in ("oneOf", "anyOf"):
        nullable_branch = _extract_nullable_branch(normalized.get(key))
        if nullable_branch is not None:
            branch, _ = nullable_branch
            merged = {k: v for k, v in normalized.items() if k != key}
            merged.update(branch)
            normalized = merged
            normalized["nullable"] = True
            break

    if "properties" in normalized and isinstance(normalized["properties"], dict):
        normalized["properties"] = {
            name: _normalize_schema_for_openai(prop) if isinstance(prop, dict) else prop
            for name, prop in normalized["properties"].items()
        }

    if "items" in normalized and isinstance(normalized["items"], dict):
        normalized["items"] = _normalize_schema_for_openai(normalized["items"])

    if normalized.get("type") != "object":
        return normalized

    normalized.setdefault("properties", {})
    normalized.setdefault("required", [])
    return normalized


# MCP 工具描述长度上限:超过此长度会截断,避免超大描述挤占上下文窗口。
_MAX_MCP_DESC_LEN = 4000

# 可疑模式:可能是 MCP 工具描述中夹带的提示注入(如 "ignore previous instructions")。
# 命中时为描述加上 `[MCP-provided]` 前缀标记,提醒后续处理注意隔离。
_MCP_SUSPICIOUS_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"ignore\s+(?:previous|prior|all)\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+(?:previous|prior|all)\s+instructions", re.IGNORECASE),
    re.compile(r"you\s+are\s+(?:now|actually)\s+", re.IGNORECASE),
    re.compile(r"system\s*prompt", re.IGNORECASE),
)


def _sanitize_mcp_description(description: str) -> str:
    """对 MCP 工具描述做安全处理:长度截断 + 可疑注入模式标记。

    - 描述过长(超过 ``_MAX_MCP_DESC_LEN``)时截断并附加 ``...[truncated]``。
    - 命中可疑提示注入模式时,在描述前加上 ``[MCP-provided]`` 前缀,
      提醒下游该描述由外部 MCP 服务器提供,需要按不可信数据处理。
    """
    if not description:
        return description
    # 长度截断
    if len(description) > _MAX_MCP_DESC_LEN:
        description = description[:_MAX_MCP_DESC_LEN] + "...[truncated]"
    # 可疑模式标记
    if any(pat.search(description) for pat in _MCP_SUSPICIOUS_PATTERNS):
        description = f"[MCP-provided] {description}"
    return description


class MCPToolWrapper(Tool):
    """Wraps a single MCP server tool as a MiniUnicorn Tool."""

    _plugin_discoverable = False

    def __init__(self, session, server_name: str, tool_def, tool_timeout: int = 30):
        self._session = session
        self._original_name = tool_def.name
        self._name = _sanitize_name(f"mcp_{server_name}_{tool_def.name}")
        self._description = _sanitize_mcp_description(tool_def.description or tool_def.name)
        raw_schema = tool_def.inputSchema or {"type": "object", "properties": {}}
        self._parameters = _normalize_schema_for_openai(raw_schema)
        self._tool_timeout = tool_timeout

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    async def execute(self, **kwargs: Any) -> str:
        from mcp import types

        def _extract_text(result: Any) -> str:
            parts: list[str] = []
            for block in result.content:
                if isinstance(block, types.TextContent):
                    parts.append(block.text)
                else:
                    parts.append(str(block))
            return "\n".join(parts) or "(no output)"

        return await _call_mcp_with_retry(
            name=self._name,
            kind="tool call",
            timeout=self._tool_timeout,
            invoke=lambda: self._session.call_tool(self._original_name, arguments=kwargs),
            extract_text=_extract_text,
        )


class MCPResourceWrapper(Tool):
    """Wraps an MCP resource URI as a read-only MiniUnicorn Tool."""

    _plugin_discoverable = False

    def __init__(self, session, server_name: str, resource_def, resource_timeout: int = 30):
        self._session = session
        self._uri = resource_def.uri
        self._name = _sanitize_name(f"mcp_{server_name}_resource_{resource_def.name}")
        desc = resource_def.description or resource_def.name
        self._description = f"[MCP Resource] {desc}\nURI: {self._uri}"
        self._parameters: dict[str, Any] = {
            "type": "object",
            "properties": {},
            "required": [],
        }
        self._resource_timeout = resource_timeout

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        from mcp import types

        def _extract_text(result: Any) -> str:
            parts: list[str] = []
            for block in result.contents:
                if isinstance(block, types.TextResourceContents):
                    parts.append(block.text)
                elif isinstance(block, types.BlobResourceContents):
                    parts.append(f"[Binary resource: {len(block.blob)} bytes]")
                else:
                    parts.append(str(block))
            return "\n".join(parts) or "(no output)"

        return await _call_mcp_with_retry(
            name=self._name,
            kind="resource read",
            timeout=self._resource_timeout,
            invoke=lambda: self._session.read_resource(self._uri),
            extract_text=_extract_text,
        )


class MCPPromptWrapper(Tool):
    """Wraps an MCP prompt as a read-only MiniUnicorn Tool."""

    _plugin_discoverable = False

    def __init__(self, session, server_name: str, prompt_def, prompt_timeout: int = 30):
        self._session = session
        self._prompt_name = prompt_def.name
        self._name = _sanitize_name(f"mcp_{server_name}_prompt_{prompt_def.name}")
        desc = prompt_def.description or prompt_def.name
        self._description = (
            f"[MCP Prompt] {desc}\n"
            "Returns a filled prompt template that can be used as a workflow guide."
        )
        self._prompt_timeout = prompt_timeout

        # Build parameters from prompt arguments
        properties: dict[str, Any] = {}
        required: list[str] = []
        for arg in prompt_def.arguments or []:
            prop: dict[str, Any] = {"type": "string"}
            if getattr(arg, "description", None):
                prop["description"] = arg.description
            properties[arg.name] = prop
            if arg.required:
                required.append(arg.name)
        self._parameters: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        from mcp import types
        from mcp.shared.exceptions import McpError

        def _extract_text(result: Any) -> str:
            parts: list[str] = []
            for message in result.messages:
                content = message.content
                if isinstance(content, types.TextContent):
                    parts.append(content.text)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, types.TextContent):
                            parts.append(block.text)
                        else:
                            parts.append(str(block))
                else:
                    parts.append(str(content))
            return "\n".join(parts) or "(no output)"

        def _handle_mcp_error(exc: BaseException, name: str, kind: str) -> str | None:
            if not isinstance(exc, McpError):
                return None
            logger.exception(
                "MCP {} '{}' failed: code={} message={}",
                kind,
                name,
                exc.error.code,
                exc.error.message,
            )
            return f"(MCP {kind} failed: {exc.error.message} [code {exc.error.code}])"

        return await _call_mcp_with_retry(
            name=self._name,
            kind="prompt call",
            timeout=self._prompt_timeout,
            invoke=lambda: self._session.get_prompt(self._prompt_name, arguments=kwargs),
            extract_text=_extract_text,
            specialized_error_handler=_handle_mcp_error,
        )


async def connect_mcp_servers(
    mcp_servers: dict, registry: ToolRegistry
) -> dict[str, AsyncExitStack]:
    """Connect to configured MCP servers and register their tools, resources, prompts.

    Returns a dict mapping server name -> its dedicated AsyncExitStack.
    Each server gets its own stack to prevent cancel scope conflicts
    when multiple MCP servers are configured.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client

    async def connect_single_server(name: str, cfg) -> tuple[str, AsyncExitStack | None]:
        server_stack = AsyncExitStack()
        await server_stack.__aenter__()

        try:
            transport_type = cfg.type
            if not transport_type:
                if cfg.command:
                    transport_type = "stdio"
                elif cfg.url:
                    transport_type = (
                        "sse" if cfg.url.rstrip("/").endswith("/sse") else "streamableHttp"
                    )
                else:
                    logger.warning("MCP server '{}': no command or url configured, skipping", name)
                    await server_stack.aclose()
                    return name, None

            if transport_type == "stdio":
                command, args, env = _normalize_windows_stdio_command(
                    cfg.command,
                    cfg.args,
                    cfg.env or None,
                )
                params = StdioServerParameters(
                    command=command,
                    args=args,
                    env=env,
                    cwd=cfg.cwd or None,
                )
                read, write = await server_stack.enter_async_context(stdio_client(params))
            elif transport_type == "sse":
                url_error = _validate_mcp_url(cfg.url)
                if url_error:
                    logger.warning("MCP server '{}': URL rejected ({}), skipping", name, url_error)
                    await server_stack.aclose()
                    return name, None
                if not await _probe_http_url(cfg.url):
                    logger.warning(
                        "MCP server '{}': {} unreachable, skipping",
                        name,
                        _redact_url_for_log(cfg.url),
                    )
                    await server_stack.aclose()
                    return name, None

                def httpx_client_factory(
                    headers: dict[str, str] | None = None,
                    timeout: httpx.Timeout | None = None,
                    auth: httpx.Auth | None = None,
                ) -> httpx.AsyncClient:
                    from miniunicorn.security.network import create_ssrf_safe_client

                    merged_headers = {
                        "Accept": "application/json, text/event-stream",
                        **(cfg.headers or {}),
                        **(headers or {}),
                    }
                    # allow_private: MCP server URLs are operator-configured;
                    # local/LAN servers are legitimate. The SSRF hook still
                    # blocks cloud metadata endpoints on every dial, including
                    # redirects followed by httpx internally.
                    return create_ssrf_safe_client(
                        allow_private=True,
                        headers=merged_headers or None,
                        timeout=timeout if timeout is not None else httpx.Timeout(10.0),
                        auth=auth,
                        follow_redirects=True,
                    )

                read, write = await server_stack.enter_async_context(
                    sse_client(cfg.url, httpx_client_factory=httpx_client_factory)
                )
            elif transport_type == "streamableHttp":
                url_error = _validate_mcp_url(cfg.url)
                if url_error:
                    logger.warning("MCP server '{}': URL rejected ({}), skipping", name, url_error)
                    await server_stack.aclose()
                    return name, None
                if not await _probe_http_url(cfg.url):
                    logger.warning(
                        "MCP server '{}': {} unreachable, skipping",
                        name,
                        _redact_url_for_log(cfg.url),
                    )
                    await server_stack.aclose()
                    return name, None

                from miniunicorn.security.network import create_ssrf_safe_client

                http_client = await server_stack.enter_async_context(
                    create_ssrf_safe_client(
                        allow_private=True,
                        headers=cfg.headers or None,
                        follow_redirects=True,
                        timeout=None,
                    )
                )
                read, write, _ = await server_stack.enter_async_context(
                    streamable_http_client(cfg.url, http_client=http_client)
                )
            else:
                logger.warning("MCP server '{}': unknown transport type '{}'", name, transport_type)
                await server_stack.aclose()
                return name, None

            session = await server_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()

            tools = await session.list_tools()
            enabled_tools = set(cfg.enabled_tools)
            allow_all_tools = "*" in enabled_tools
            registered_count = 0
            matched_enabled_tools: set[str] = set()
            available_raw_names = [tool_def.name for tool_def in tools.tools]
            available_wrapped_names = [
                _sanitize_name(f"mcp_{name}_{tool_def.name}") for tool_def in tools.tools
            ]
            for tool_def in tools.tools:
                wrapped_name = _sanitize_name(f"mcp_{name}_{tool_def.name}")
                if (
                    not allow_all_tools
                    and tool_def.name not in enabled_tools
                    and wrapped_name not in enabled_tools
                ):
                    logger.debug(
                        "MCP: skipping tool '{}' from server '{}' (not in enabledTools)",
                        wrapped_name,
                        name,
                    )
                    continue
                wrapper = MCPToolWrapper(session, name, tool_def, tool_timeout=cfg.tool_timeout)
                registry.register(wrapper)
                logger.debug("MCP: registered tool '{}' from server '{}'", wrapper.name, name)
                registered_count += 1
                if enabled_tools:
                    if tool_def.name in enabled_tools:
                        matched_enabled_tools.add(tool_def.name)
                    if wrapped_name in enabled_tools:
                        matched_enabled_tools.add(wrapped_name)

            if enabled_tools and not allow_all_tools:
                unmatched_enabled_tools = sorted(enabled_tools - matched_enabled_tools)
                if unmatched_enabled_tools:
                    logger.warning(
                        "MCP server '{}': enabledTools entries not found: {}. Available raw names: {}. "
                        "Available wrapped names: {}",
                        name,
                        ", ".join(unmatched_enabled_tools),
                        ", ".join(available_raw_names) or "(none)",
                        ", ".join(available_wrapped_names) or "(none)",
                    )

            try:
                resources_result = await session.list_resources()
                for resource in resources_result.resources:
                    wrapper = MCPResourceWrapper(
                        session, name, resource, resource_timeout=cfg.tool_timeout
                    )
                    registry.register(wrapper)
                    registered_count += 1
                    logger.debug(
                        "MCP: registered resource '{}' from server '{}'", wrapper.name, name
                    )
            except Exception as e:
                logger.debug("MCP server '{}': resources not supported or failed: {}", name, e)

            try:
                prompts_result = await session.list_prompts()
                for prompt in prompts_result.prompts:
                    wrapper = MCPPromptWrapper(
                        session, name, prompt, prompt_timeout=cfg.tool_timeout
                    )
                    registry.register(wrapper)
                    registered_count += 1
                    logger.debug("MCP: registered prompt '{}' from server '{}'", wrapper.name, name)
            except Exception as e:
                logger.debug("MCP server '{}': prompts not supported or failed: {}", name, e)

            logger.info(
                "MCP server '{}': connected, {} capabilities registered", name, registered_count
            )
            return name, server_stack

        except Exception as e:
            hint = ""
            text = str(e).lower()
            if any(
                marker in text
                for marker in (
                    "parse error",
                    "invalid json",
                    "unexpected token",
                    "jsonrpc",
                    "content-length",
                )
            ):
                hint = (
                    " Hint: this looks like stdio protocol pollution. Make sure the MCP server writes "
                    "only JSON-RPC to stdout and sends logs/debug output to stderr instead."
                )
            logger.exception("MCP server '{}': failed to connect: {}", name, hint)
            with suppress(Exception):
                await server_stack.aclose()
            return name, None

    server_stacks: dict[str, AsyncExitStack] = {}

    for name, cfg in mcp_servers.items():
        try:
            result = await connect_single_server(name, cfg)
        except Exception as e:
            logger.exception("MCP server '{}' connection failed: {}", name, e)
            continue
        if result is not None and result[1] is not None:
            server_stacks[result[0]] = result[1]

    return server_stacks


def session_extra(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return persisted session kwargs for MCP preset attachments."""
    mcp_presets = metadata.get("mcp_presets") if isinstance(metadata, Mapping) else None
    return {"mcp_presets": mcp_presets} if isinstance(mcp_presets, list) and mcp_presets else {}


def runtime_lines(
    message: Any,
    *,
    available_server_names: set[str] | None = None,
    configured_server_names: set[str] | None = None,
    connected_server_names: set[str] | None = None,
    skip: bool = False,
) -> list[str]:
    """Return model-visible MCP preset annotations for the current turn."""
    if skip:
        return []
    if configured_server_names is None:
        configured_server_names = available_server_names
    if connected_server_names is None:
        connected_server_names = available_server_names
    metadata = message.metadata if isinstance(getattr(message, "metadata", None), Mapping) else None
    structured = metadata.get("mcp_presets") if isinstance(metadata, Mapping) else None
    if not isinstance(structured, list):
        return []

    lines: list[str] = []
    for item in structured[:8]:
        if not isinstance(item, Mapping):
            continue
        raw_name = str(item.get("name") or "").strip().lower()
        if not raw_name:
            continue
        display = str(item.get("display_name") or raw_name).strip() or raw_name
        transport = str(item.get("transport") or "mcp").strip() or "mcp"
        prefix = f"mcp_{raw_name}_"
        if configured_server_names is not None and raw_name not in configured_server_names:
            lines.append(
                "MCP Preset Attachment: "
                f"@{raw_name} ({display}; transport={transport}) is configured in WebUI Settings, "
                "but this gateway has not loaded the latest MCP settings yet. "
                f"Tools with prefix `{prefix}` may not be available yet; if they are missing, "
                "tell the user to restart MiniUnicorn."
            )
            continue
        if connected_server_names is not None and raw_name not in connected_server_names:
            lines.append(
                "MCP Preset Attachment: "
                f"@{raw_name} ({display}; transport={transport}) is configured, "
                "but its MCP connection is not currently live. "
                f"Tools with prefix `{prefix}` may be unavailable; tell the user to open Settings, "
                "run the preset test, and restart MiniUnicorn only if hot reload is unavailable."
            )
            continue
        lines.append(
            "MCP Preset Attachment: "
            f"@{raw_name} ({display}; transport={transport}; tool_prefix={prefix}). "
            f"Prefer available tools whose names start with `{prefix}` for this request; "
            "do not substitute shell commands for this MCP integration unless the user asks."
        )
    return lines


async def connect_missing_servers(state: RuntimeState, registry: ToolRegistry) -> None:
    """Connect configured MCP servers that are not currently live."""
    missing_servers = {
        name: cfg for name, cfg in state._mcp_servers.items() if name not in state._mcp_stacks
    }
    if state._mcp_connecting or not missing_servers:
        return
    state._mcp_connecting = True
    try:
        connected = await connect_mcp_servers(missing_servers, registry)
        state._mcp_stacks.update(connected)
        state._mcp_connected = bool(state._mcp_stacks)
        if connected:
            logger.info("MCP connected servers: {}", sorted(connected))
        else:
            logger.warning("No MCP servers connected successfully (will retry next message)")
    except asyncio.CancelledError:
        logger.warning("MCP connection cancelled (will retry next message)")
        state._mcp_connected = bool(state._mcp_stacks)
    except BaseException as e:
        logger.warning("Failed to connect MCP servers (will retry next message): {}", e)
        state._mcp_connected = bool(state._mcp_stacks)
    finally:
        state._mcp_connecting = False


async def reload_servers(state: RuntimeState, registry: ToolRegistry) -> dict[str, Any]:
    """Reconcile live MCP connections with the current config file."""
    async with _reload_lock(state):
        try:
            from miniunicorn.config.loader import load_config, resolve_config_env_vars

            config = resolve_config_env_vars(load_config())
            next_servers = dict(config.tools.mcp_servers)
        except Exception as exc:
            logger.warning("MCP hot reload could not read config: {}", exc)
            return {
                "ok": False,
                "message": "Could not reload MCP config. Restart MiniUnicorn to pick up changes.",
                "requires_restart": True,
                "error": str(exc),
            }

        current_servers = dict(state._mcp_servers)
        current_names = set(current_servers)
        next_names = set(next_servers)
        removed = sorted(current_names - next_names)
        added = sorted(next_names - current_names)
        changed = sorted(
            name
            for name in current_names & next_names
            if _server_signature(current_servers[name]) != _server_signature(next_servers[name])
        )

        tools_removed = 0
        for name in [*removed, *changed]:
            tools_removed += _unregister_server_tools(state, registry, name)
            await _close_server(state, name)

        state._set_mcp_servers(next_servers)
        retry_missing = sorted(
            name
            for name in next_names
            if name not in state._mcp_stacks and name not in set(added) | set(changed)
        )
        to_connect_names = sorted(set(added) | set(changed) | set(retry_missing))
        to_connect = {name: next_servers[name] for name in to_connect_names}
        connected: dict[str, AsyncExitStack] = {}
        if to_connect:
            connected = await connect_mcp_servers(to_connect, registry)
            state._mcp_stacks.update(connected)

        state._mcp_connected = bool(state._mcp_stacks)
        failed = sorted(set(to_connect) - set(connected))
        unchanged = not removed and not added and not changed and not retry_missing
        ok = not failed
        if failed:
            message = "MCP config reloaded, but some servers did not connect: " + ", ".join(failed)
        elif unchanged:
            message = "MCP config is already live."
        elif retry_missing and not added and not changed and not removed:
            message = "MCP connections refreshed without restarting MiniUnicorn."
        else:
            message = "MCP config reloaded without restarting MiniUnicorn."

        logger.info(
            "MCP hot reload: added={} changed={} removed={} retried={} connected={} failed={} tools_removed={}",
            added,
            changed,
            removed,
            retry_missing,
            sorted(connected),
            failed,
            tools_removed,
        )
        return {
            "ok": ok,
            "message": message,
            "added": added,
            "changed": changed,
            "removed": removed,
            "retried": retry_missing,
            "connected": sorted(state._mcp_stacks),
            "configured": sorted(state._mcp_servers),
            "failed": failed,
            "tools_removed": tools_removed,
            "requires_restart": False,
        }


async def request_mcp_reload(bus: Any, *, timeout: float = 15.0) -> dict[str, Any]:
    """Ask the running agent loop to reconcile live MCP connections."""
    loop = asyncio.get_running_loop()
    ack: asyncio.Future[dict[str, Any]] = loop.create_future()
    await bus.publish_inbound(
        InboundMessage(
            channel="system",
            sender_id="webui-settings",
            chat_id="runtime",
            content=RUNTIME_CONTROL_MCP_RELOAD,
            metadata={
                INBOUND_META_RUNTIME_CONTROL: RUNTIME_CONTROL_MCP_RELOAD,
                RUNTIME_CONTROL_ACK: ack,
            },
        )
    )
    try:
        result = await asyncio.wait_for(ack, timeout=timeout)
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "message": "MCP hot reload timed out. Restart MiniUnicorn to pick up changes.",
            "requires_restart": True,
        }
    return (
        result
        if isinstance(result, dict)
        else {
            "ok": False,
            "message": "MCP hot reload returned an unexpected response.",
            "requires_restart": True,
        }
    )


async def handle_runtime_control(
    state: RuntimeState, msg: InboundMessage, registry: ToolRegistry
) -> bool:
    metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
    control = metadata.get(INBOUND_META_RUNTIME_CONTROL)
    if control != RUNTIME_CONTROL_MCP_RELOAD:
        return False

    ack = metadata.get(RUNTIME_CONTROL_ACK)
    try:
        result = await reload_servers(state, registry)
    except Exception as exc:
        logger.exception("MCP hot reload failed")
        result = {
            "ok": False,
            "message": "MCP hot reload failed. Restart MiniUnicorn to pick up changes.",
            "requires_restart": True,
            "error": str(exc),
        }
    if isinstance(ack, asyncio.Future) and not ack.done():
        ack.set_result(result)
    return True


def _reload_lock(state: RuntimeState) -> asyncio.Lock:
    try:
        return _RELOAD_LOCKS[state]
    except KeyError:
        lock = asyncio.Lock()
        _RELOAD_LOCKS[state] = lock
        return lock


def _server_signature(cfg: Any) -> Any:
    if hasattr(cfg, "model_dump"):
        return cfg.model_dump(mode="json")
    return cfg


def _tool_prefix(server_name: str) -> str:
    safe_name = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in server_name)
    while "__" in safe_name:
        safe_name = safe_name.replace("__", "_")
    return f"mcp_{safe_name}_"


def _unregister_server_tools(state: RuntimeState, registry: ToolRegistry, server_name: str) -> int:
    prefix = _tool_prefix(server_name)
    removed = 0
    for tool_name in list(registry.tool_names):
        if tool_name.startswith(prefix):
            registry.unregister(tool_name)
            removed += 1
    return removed


async def _close_server(state: RuntimeState, server_name: str) -> None:
    stack = state._mcp_stacks.pop(server_name, None)
    if stack is None:
        return
    try:
        await stack.aclose()
    except (RuntimeError, BaseExceptionGroup):
        logger.debug("MCP server '{}' cleanup error (can be ignored)", server_name)
