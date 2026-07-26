"""WebSocket channel package (WebUI server + REST API host).

Backward-compat: re-exports everything from ``.channel`` so legacy
``from miniunicorn.channels.websocket import WebSocketChannel`` keeps working.
"""
from .channel import *  # noqa: F401,F403  — public symbols
from .channel import (  # noqa: F401  — private names used by tests/monkeypatch
    _default_model_name_from_config,
    _extract_data_url_mime,
    _is_valid_chat_id,
    _issue_route_secret_matches,
    _normalize_config_path,
    _normalize_http_path,
    _parse_envelope,
    _parse_inbound_payload,
    _parse_query,
    _parse_request_path,
)

__all__ = [
    "WebSocketChannel",
    "WebSocketConfig",
    "publish_runtime_model_update",
    "channel",
]
