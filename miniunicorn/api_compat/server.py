"""OpenAI-compatible HTTP API server for a fixed MiniUnicorn session.

Provides /v1/chat/completions and /v1/models endpoints.
All requests route to a single persistent API session.

This module belongs to the optional ``api`` extra (imports ``aiohttp``).
The only supported entry point is ``miniunicorn serve``, which imports it
lazily so the core install never requires aiohttp.

CORS 说明
---------
本服务器**不**处理 CORS 预检请求,也不返回 ``Access-Control-*`` 响应头。
设计意图是仅供同源调用或服务端到服务端调用(如 Python ``requests``、
OpenAI SDK、curl 等)。如需浏览器直连:

1. 推荐方案:在反向代理(nginx/caddy/traefik)层处理 CORS,并将请求
   转发到本服务。这样安全策略集中可控,且能附加额外的认证/限流。
2. 若必须由应用层处理,可在 ``create_app`` 返回的 ``web.Application``
   上叠加 ``aiohttp_cors`` 中间件,但需自行评估 CSRF/凭证泄露风险。

注意:即便加上 CORS,也务必配合 ``api.api_key`` 一起使用,不要把无认证
的 API 暴露到公网(参考 ``serve`` 命令的 warning 输出)。
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json as _json
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from aiohttp import web
from loguru import logger

from miniunicorn.config.paths import get_media_dir
from miniunicorn.utils.helpers import safe_filename
from miniunicorn.utils.media_decode import (
    MAX_FILE_SIZE,
)
from miniunicorn.utils.media_decode import (
    FileSizeExceededError as _FileSizeExceededError,
)
from miniunicorn.utils.media_decode import (
    save_base64_data_url as _save_base64_data_url,
)
from miniunicorn.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

__all__ = (
    "MAX_FILE_SIZE",
    "_FileSizeExceededError",
    "_save_base64_data_url",
    "create_app",
    "handle_chat_completions",
)


API_SESSION_KEY = "api:default"
API_CHAT_ID = "default"

# Routes that bypass auth (health checks only). /v1/* always requires auth
# when api_key is configured.
_PUBLIC_PATHS = frozenset({"/health"})

# session_locks LRU 上限,避免恶意/异常客户端用大量不同 session_id 耗尽内存。
# 超过上限时驱逐最久未使用的 session 锁(仅驱逐引用计数为零且未持有的锁)。
_MAX_SESSION_LOCKS = 1000


@dataclass
class _SessionLockEntry:
    """带引用计数的 session 锁条目。

    LRU 驱逐只淘汰 ``refcount == 0`` 且锁未被持有的条目,避免正在等待锁的
    请求因锁被驱逐而绕过串行化(两个并发请求为同一 session 各自创建新锁)。
    """

    lock: asyncio.Lock
    refcount: int = 0

    def is_idle(self) -> bool:
        """Return True when not held and no waiters (safe to evict)."""
        return self.refcount == 0 and not self.lock.locked()


def _enforce_session_lock_limit(
    session_locks: "OrderedDict[str, _SessionLockEntry]",
) -> None:
    """驱逐最旧的可淘汰 session 锁条目,直到满足 _MAX_SESSION_LOCKS 上限。

    只淘汰 ``is_idle()`` 为真的条目(refcount==0 且锁未持有)。若所有条目
    都在使用中,跳过驱逐(宁可暂时超限也不破坏串行化)。
    """
    while len(session_locks) > _MAX_SESSION_LOCKS:
        # 找到最旧的可淘汰条目
        evicted_key: str | None = None
        for key, entry in session_locks.items():
            if entry.is_idle():
                evicted_key = key
                break
        if evicted_key is None:
            # 所有条目都在使用中,跳过驱逐
            break
        session_locks.pop(evicted_key, None)


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _error_json(status: int, message: str, err_type: str = "invalid_request_error") -> web.Response:
    return web.json_response(
        {"error": {"message": message, "type": err_type, "code": status}},
        status=status,
    )


def _chat_completion_response(content: str, model: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _response_text(value: Any) -> str:
    """Normalize process_direct output to plain assistant text."""
    if value is None:
        return ""
    if hasattr(value, "content"):
        return str(getattr(value, "content") or "")
    return str(value)


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def _sse_chunk(delta: str, model: str, chunk_id: str, finish_reason: str | None = None) -> bytes:
    """Format a single OpenAI-compatible SSE chunk."""
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": delta} if delta else {},
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {_json.dumps(payload)}\n\n".encode()


_SSE_DONE = b"data: [DONE]\n\n"

# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------


def _parse_json_content(body: dict) -> tuple[str, list[str]]:
    """Parse JSON request body. Returns (text, media_paths)."""
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) != 1:
        raise ValueError("Only a single user message is supported")
    message = messages[0]
    if not isinstance(message, dict) or message.get("role") != "user":
        raise ValueError("Only a single user message is supported")

    user_content = message.get("content", "")
    media_dir = get_media_dir("api")
    media_paths: list[str] = []

    if isinstance(user_content, list):
        text_parts: list[str] = []
        for part in user_content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text_parts.append(part.get("text", ""))
            elif part.get("type") == "image_url":
                url = part.get("image_url", {}).get("url", "")
                if url.startswith("data:"):
                    saved = _save_base64_data_url(url, media_dir)
                    if saved:
                        media_paths.append(saved)
                elif url:
                    raise ValueError(
                        "Remote image URLs are not supported. "
                        "Use base64 data URLs or upload files via multipart/form-data."
                    )
        text = " ".join(text_parts)
    elif isinstance(user_content, str):
        text = user_content
    else:
        raise ValueError("Invalid content format")

    return text, media_paths


async def _parse_multipart(request: web.Request) -> tuple[str, list[str], str | None, str | None]:
    """Parse multipart/form-data. Returns (text, media_paths, session_id, model)."""
    media_dir = get_media_dir("api")
    reader = await request.multipart()
    text = ""
    session_id = None
    model = None
    media_paths: list[str] = []

    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "message":
            text = (await part.read()).decode("utf-8")
        elif part.name == "session_id":
            session_id = (await part.read()).decode("utf-8").strip()
        elif part.name == "model":
            model = (await part.read()).decode("utf-8").strip()
        elif part.name == "files":
            # 流式读取并写盘,避免将整个文件缓冲到内存后才校验尺寸。
            # 一旦累计字节数超过 MAX_FILE_SIZE 立即停止并抛出异常,
            # 防止恶意客户端通过超大 body 耗尽服务器内存。
            base = safe_filename(part.filename or "upload.bin")
            filename = f"{uuid.uuid4().hex[:12]}_{base}"
            dest = media_dir / filename
            written = 0
            try:
                with open(dest, "wb") as f:
                    while True:
                        chunk = await part.read_chunk(64 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > MAX_FILE_SIZE:
                            raise _FileSizeExceededError(
                                f"File '{part.filename}' exceeds {MAX_FILE_SIZE // (1024 * 1024)}MB limit"
                            )
                        f.write(chunk)
            except BaseException:
                # 清理未完成的临时文件,避免半成品占用磁盘。
                with contextlib.suppress(OSError):
                    dest.unlink(missing_ok=True)
                raise
            media_paths.append(str(dest))

    if not text:
        text = "请分析上传的文件"

    return text, media_paths, session_id, model


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def handle_chat_completions(request: web.Request) -> web.Response:
    """POST /v1/chat/completions — supports JSON and multipart/form-data."""
    content_type = request.content_type or ""
    if not isinstance(content_type, str):
        content_type = ""

    agent_loop = request.app["agent_loop"]
    timeout_s: float = request.app.get("request_timeout", 120.0)
    model_name: str = request.app.get("model_name", "MiniUnicorn")

    stream = False
    try:
        if content_type.startswith("multipart/"):
            text, media_paths, session_id, requested_model = await _parse_multipart(request)
        else:
            try:
                body = await request.json()
            except Exception:
                return _error_json(400, "Invalid JSON body")
            stream = body.get("stream", False)
            requested_model = body.get("model")
            text, media_paths = _parse_json_content(body)
            session_id = body.get("session_id")
    except ValueError as e:
        return _error_json(400, str(e))
    except _FileSizeExceededError as e:
        return _error_json(413, str(e), err_type="invalid_request_error")
    except Exception:
        logger.exception("Error parsing upload")
        return _error_json(413, "File too large or invalid upload")

    if requested_model and requested_model != model_name:
        return _error_json(400, f"Only configured model '{model_name}' is available")

    session_key = f"api:{session_id}" if session_id else API_SESSION_KEY
    session_locks: "OrderedDict[str, _SessionLockEntry]" = request.app["session_locks"]
    # LRU 策略:命中则移到末尾 (最近使用);未命中则新建并在超出上限时驱逐最旧。
    # 引用计数:请求在等待锁之前 +1,退出后 -1;LRU 只淘汰 refcount==0 且未持有的条目,
    # 避免正在等待锁的请求因锁被驱逐而绕过串行化。
    entry = session_locks.get(session_key)
    if entry is None:
        entry = _SessionLockEntry(lock=asyncio.Lock())
        session_locks[session_key] = entry
        _enforce_session_lock_limit(session_locks)
    else:
        session_locks.move_to_end(session_key)
    entry.refcount += 1
    session_lock = entry.lock

    try:
        logger.info(
            "API request session_key={} media={} text={} stream={}",
            session_key,
            len(media_paths),
            text[:80],
            stream,
        )
        # -- streaming path --
        if stream:
            resp = web.StreamResponse()
            resp.content_type = "text/event-stream"
            resp.headers["Cache-Control"] = "no-cache"
            resp.headers["Connection"] = "keep-alive"
            await resp.prepare(request)

            chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
            queue: asyncio.Queue[str | None] = asyncio.Queue()
            stream_failed = False
            emitted_content = False

            async def _on_stream(token: str) -> None:
                nonlocal emitted_content
                if token:
                    emitted_content = True
                await queue.put(token)

            async def _on_stream_end(*_a: Any, **_kw: Any) -> None:
                # Agent stream-end callbacks mark generation segment boundaries.
                # Tool-backed requests may continue after a segment ends, so the
                # HTTP SSE stream is closed only when process_direct returns.
                return None

            async def _run() -> None:
                nonlocal stream_failed
                try:
                    async with session_lock:
                        response = await asyncio.wait_for(
                            agent_loop.process_direct(
                                content=text,
                                media=media_paths if media_paths else None,
                                session_key=session_key,
                                channel="api",
                                chat_id=API_CHAT_ID,
                                on_stream=_on_stream,
                                on_stream_end=_on_stream_end,
                            ),
                            timeout=timeout_s,
                        )
                        if not emitted_content:
                            response_text = _response_text(response)
                            if response_text.strip():
                                await queue.put(response_text)
                except Exception:
                    stream_failed = True
                    logger.exception("Streaming error for session {}", session_key)
                finally:
                    await queue.put(None)

            task = asyncio.create_task(_run())
            try:
                while True:
                    token = await queue.get()
                    if token is None:
                        break
                    await resp.write(_sse_chunk(token, model_name, chunk_id))
            finally:
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

            if not stream_failed:
                await resp.write(_sse_chunk("", model_name, chunk_id, finish_reason="stop"))
                await resp.write(_SSE_DONE)
            return resp

        # -- non-streaming path (original logic) --
        fallback = EMPTY_FINAL_RESPONSE_MESSAGE

        try:
            async with session_lock:
                try:
                    response = await asyncio.wait_for(
                        agent_loop.process_direct(
                            content=text,
                            media=media_paths if media_paths else None,
                            session_key=session_key,
                            channel="api",
                            chat_id=API_CHAT_ID,
                        ),
                        timeout=timeout_s,
                    )
                    response_text = _response_text(response)

                    if not response_text or not response_text.strip():
                        logger.warning("Empty response for session {}, retrying", session_key)
                        retry_response = await asyncio.wait_for(
                            agent_loop.process_direct(
                                content=text,
                                media=media_paths if media_paths else None,
                                session_key=session_key,
                                channel="api",
                                chat_id=API_CHAT_ID,
                            ),
                            timeout=timeout_s,
                        )
                        response_text = _response_text(retry_response)
                        if not response_text or not response_text.strip():
                            logger.warning("Empty response after retry, using fallback")
                            response_text = fallback

                except asyncio.TimeoutError:
                    return _error_json(504, f"Request timed out after {timeout_s}s")
                except Exception:
                    logger.exception("Error processing request for session {}", session_key)
                    return _error_json(500, "Internal server error", err_type="server_error")
        except Exception:
            logger.exception("Unexpected API lock error for session {}", session_key)
            return _error_json(500, "Internal server error", err_type="server_error")

        return web.json_response(_chat_completion_response(response_text, model_name))
    finally:
        entry.refcount -= 1


async def handle_models(request: web.Request) -> web.Response:
    """GET /v1/models"""
    model_name = request.app.get("model_name", "MiniUnicorn")
    return web.json_response(
        {
            "object": "list",
            "data": [
                {
                    "id": model_name,
                    "object": "model",
                    "created": 0,
                    "owned_by": "MiniUnicorn",
                }
            ],
        }
    )


async def handle_health(request: web.Request) -> web.Response:
    """GET /health"""
    return web.json_response({"status": "ok"})


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


@web.middleware
async def _auth_middleware(request: web.Request, handler):
    """Bearer-token auth for /v1/* endpoints.

    - ``/health`` is always public (liveness probes).
    - When ``app["api_key"]`` is empty, all routes are open (development mode).
    - When set, ``/v1/*`` requires ``Authorization: Bearer <api_key>``.
    Comparison uses ``hmac.compare_digest`` to avoid timing leaks.
    """
    api_key: str = request.app.get("api_key", "")
    if not api_key:
        return await handler(request)

    path = request.path
    # Strip trailing slash for matching (except root).
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    if path in _PUBLIC_PATHS:
        return await handler(request)

    auth = request.headers.get("Authorization") or request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    else:
        supplied = ""

    if supplied and hmac.compare_digest(supplied, api_key):
        return await handler(request)

    return _error_json(
        401,
        "Missing or invalid Authorization header. Expected: 'Authorization: Bearer <api_key>'",
        err_type="authentication_error",
    )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    agent_loop,
    model_name: str = "MiniUnicorn",
    request_timeout: float = 120.0,
    api_key: str = "",
) -> web.Application:
    """Create the aiohttp application.

    Args:
        agent_loop: An initialized AgentLoop instance.
        model_name: Model name reported in responses.
        request_timeout: Per-request timeout in seconds.
        api_key: Bearer token required for /v1/* endpoints. Empty = no auth.
    """
    app = web.Application(
        client_max_size=20 * 1024 * 1024,  # 20MB for base64 images
        middlewares=[_auth_middleware],
    )
    app["agent_loop"] = agent_loop
    app["model_name"] = model_name
    app["request_timeout"] = request_timeout
    app["api_key"] = api_key
    # 使用 OrderedDict 实现 LRU:命中时 move_to_end,超过 _MAX_SESSION_LOCKS
    # 时 popitem(last=False) 驱逐最久未使用的 session 锁。
    app["session_locks"] = OrderedDict()  # per-user locks, keyed by session_key

    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/health", handle_health)
    return app
