"""Process-safe OpenAI-compatible HTTP stub for supervised runtime tests.

Provides a :class:`ThreadingHTTPServer` fixture that binds ``127.0.0.1`` on
an ephemeral port, records request bodies under a lock, and returns
deterministic Chat Completions responses. Used by the supervised golden
flow to drive a real spawned Worker against a local Provider endpoint
without depending on any external network (Task 1 Step 1).

Handles both non-streaming (JSON) and streaming (SSE) requests. The
production Worker binds a ``progress_port`` which causes the Agent loop to
use streaming; the stub must return SSE chunks in that case or the SDK
silently drops the response content.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class OpenAIStubServer:
    """Thread-safe OpenAI-compatible Chat Completions stub.

    Construct with a list of response dicts; each POST pops the next
    response in order. ``requests`` records every received body under a
    lock so tests can assert exactly one Provider call.
    """

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._request_count = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    body = json.loads(raw_body or b"{}")
                except json.JSONDecodeError:
                    body = {"_raw": raw_body.decode("utf-8", errors="replace")}
                with owner._lock:
                    owner.requests.append(body)
                    owner._request_count += 1
                    response = (
                        owner._responses.pop(0)
                        if owner._responses
                        else chat_completion("fallback")
                    )

                if body.get("stream"):
                    chunks = _stream_chunks(response)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    for chunk in chunks:
                        self.wfile.write(f"data: {chunk}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                else:
                    raw = json.dumps(response).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

            def log_message(self, *_args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )

    @property
    def api_base(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1"

    @property
    def request_count(self) -> int:
        with self._lock:
            return self._request_count

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def chat_completion(content: str) -> dict[str, Any]:
    """Build a deterministic Chat Completions response for *content*.

    Includes ``id``, ``object``, ``created``, ``model``, one assistant
    choice with ``finish_reason="stop"``, and usage token counts.
    """
    return {
        "id": f"chatcmpl-stub-{abs(hash(content)) % 10_000_000}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "stub-model",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": max(1, len(content.split())),
            "total_tokens": 10 + max(1, len(content.split())),
        },
    }


def _stream_chunks(response: dict[str, Any]) -> list[str]:
    """Convert a non-streaming response into SSE chunk dicts.

    The OpenAI streaming format sends the content as ``delta`` increments:
    1. First chunk: ``delta: {"role": "assistant", "content": ""}``
    2. Content chunks: ``delta: {"content": <text>}``
    3. Final chunk: ``delta: {}, finish_reason: "stop"``, plus ``usage``.
    """
    chunk_id = response.get("id", "chatcmpl-stub")
    created = response.get("created", int(time.time()))
    model = response.get("model", "stub-model")
    choices = response.get("choices", [])
    choice0 = choices[0] if choices else {}
    content = (choice0.get("message") or {}).get("content", "") or ""
    usage = response.get("usage", {})

    base: dict[str, Any] = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
    }

    chunks: list[str] = []
    # First chunk: role + empty content.
    chunks.append(
        json.dumps(
            {
                **base,
                "choices": [
                    {"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}
                ],
            }
        )
    )
    # Content chunk(s): send the full content in one delta.
    if content:
        chunks.append(
            json.dumps(
                {
                    **base,
                    "choices": [
                        {"index": 0, "delta": {"content": content}, "finish_reason": None}
                    ],
                }
            )
        )
    # Final chunk: empty delta, finish_reason, and usage.
    final_chunk: dict[str, Any] = {
        **base,
        "choices": [
            {"index": 0, "delta": {}, "finish_reason": "stop"}
        ],
    }
    if usage:
        final_chunk["usage"] = usage
    chunks.append(json.dumps(final_chunk))
    return chunks


__all__ = ["OpenAIStubServer", "chat_completion"]
