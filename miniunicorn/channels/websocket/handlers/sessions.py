"""会话消息/线程/删除/回退 handler(正则路由)。"""

from __future__ import annotations

from pathlib import Path

from websockets.http11 import Response

from miniunicorn.session.webui_turns import websocket_turn_wall_started_at
from miniunicorn.utils.subagent_channel_display import scrub_subagent_messages_for_channel
from miniunicorn.webui.thread_disk import delete_webui_thread
from miniunicorn.webui.transcript import (
    build_webui_thread_response,
    rewrite_local_markdown_images,
    rewind_webui_transcript_to_user,
)

from .._http_router import RouteContext, RouteDeps, router
from .._http_routes import (
    _decode_api_key,
    _http_error,
    _http_json_response,
    _query_first,
)
from ._common import require_auth, service_unavailable


def _is_ws_session_key(key: str) -> bool:
    """True when *key* is a ``websocket:…`` session exposed on this HTTP surface."""
    return key.startswith("websocket:")


def _augment_media_urls(payload: dict, deps: RouteDeps) -> None:
    """Mutate *payload* in place: each message's ``media`` path list is
    replaced by a parallel ``media_urls`` list of signed fetch URLs.

    Messages without media or with non-string path entries are left
    untouched. Paths that no longer live inside ``media_dir`` (e.g. the
    file was deleted, or the dir was relocated) are silently skipped;
    the client falls back to the historical-replay placeholder tile.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        media = msg.get("media")
        if not isinstance(media, list) or not media:
            continue
        urls: list[dict[str, str]] = []
        for entry in media:
            if not isinstance(entry, str) or not entry:
                continue
            signed = deps.sign_media_path(Path(entry))
            if signed is None:
                continue
            urls.append({"url": signed, "name": Path(entry).name})
        if urls:
            msg["media_urls"] = urls
        # Always drop the raw paths from the wire payload.
        msg.pop("media", None)


def _augment_transcript_user_media(
    paths: list[str], deps: RouteDeps
) -> list[dict]:
    import mimetypes

    out: list[dict] = []
    for pstr in paths:
        path = Path(pstr)
        att = deps.sign_or_stage_media_path(path)
        if att is None:
            continue
        mime, _ = mimetypes.guess_type(path.name)
        kind = "video" if mime and mime.startswith("video/") else "image"
        out.append(
            {"kind": kind, "url": att["url"], "name": att.get("name", path.name)},
        )
    return out


@router.route(r"^/api/sessions/(?P<key>[^/]+)/messages$", regex=True)
@require_auth
def session_messages(ctx: RouteContext) -> Response:
    if ctx.deps.session_manager is None:
        return service_unavailable("session manager unavailable")
    decoded_key = _decode_api_key(ctx.path_vars["key"])
    if decoded_key is None:
        return _http_error(400, "invalid session key")
    # Only ``websocket:…`` sessions are listed/served here — same boundary as
    # ``/api/sessions``. Block handcrafted URLs from probing CLI / Slack / etc.
    if not _is_ws_session_key(decoded_key):
        return _http_error(404, "session not found")
    data = ctx.deps.session_manager.read_session_file(decoded_key)
    if data is None:
        return _http_error(404, "session not found")
    messages = data.get("messages")
    if isinstance(messages, list):
        scrub_subagent_messages_for_channel(messages)
    # Decorate persisted user messages with signed media URLs so the
    # client can render previews. The raw on-disk ``media`` paths are
    # stripped on the way out — they leak server filesystem layout and
    # the client never needs them once it has the signed fetch URL.
    _augment_media_urls(data, ctx.deps)
    return _http_json_response(data)


@router.route(r"^/api/sessions/(?P<key>[^/]+)/webui-thread$", regex=True)
@require_auth
def webui_thread_get(ctx: RouteContext) -> Response:
    decoded_key = _decode_api_key(ctx.path_vars["key"])
    if decoded_key is None:
        return _http_error(400, "invalid session key")
    if not _is_ws_session_key(decoded_key):
        return _http_error(404, "session not found")
    scope = ctx.deps.webui_workspaces.scope_for_session_key(decoded_key)
    data = build_webui_thread_response(
        decoded_key,
        augment_user_media=lambda paths: _augment_transcript_user_media(paths, ctx.deps),
        augment_assistant_text=lambda text: rewrite_local_markdown_images(
            text,
            workspace_path=scope.project_path,
            sign_path=ctx.deps.sign_or_stage_media_path,
        ),
    )
    if data is None:
        return _http_error(404, "webui thread not found")
    data["workspace_scope"] = scope.payload()
    return _http_json_response(data)


@router.route(r"^/api/sessions/(?P<key>[^/]+)/delete$", regex=True)
@require_auth
def session_delete(ctx: RouteContext) -> Response:
    if ctx.deps.session_manager is None:
        return service_unavailable("session manager unavailable")
    decoded_key = _decode_api_key(ctx.path_vars["key"])
    if decoded_key is None:
        return _http_error(400, "invalid session key")
    # Same boundary as ``session_messages``: mutations apply only to
    # websocket-channel sessions; deletion unlinks local JSONL — keep scope narrow.
    if not _is_ws_session_key(decoded_key):
        return _http_error(404, "session not found")
    deleted = ctx.deps.session_manager.delete_session(decoded_key)
    delete_webui_thread(decoded_key)
    return _http_json_response({"deleted": bool(deleted)})


@router.route(r"^/api/sessions/(?P<key>[^/]+)/rewind$", regex=True)
@require_auth
def session_rewind(ctx: RouteContext) -> Response:
    """Truncate a websocket session to before the N-th user message.

    Query parameter ``user_message_index`` (0-based) identifies the user
    turn to rewind from. Both the JSONL transcript and the agent session
    file are truncated in lockstep so the WebUI and the LLM context stay
    consistent. A ``session_updated`` broadcast is emitted so connected
    clients refresh their local thread view.
    """
    if ctx.deps.session_manager is None:
        return service_unavailable("session manager unavailable")
    decoded_key = _decode_api_key(ctx.path_vars["key"])
    if decoded_key is None:
        return _http_error(400, "invalid session key")
    if not _is_ws_session_key(decoded_key):
        return _http_error(404, "session not found")
    raw_index = _query_first(ctx.query, "user_message_index")
    if raw_index is None:
        return _http_error(400, "missing user_message_index")
    try:
        user_message_index = int(raw_index)
    except ValueError:
        return _http_error(400, "user_message_index must be an integer")
    if user_message_index < 0:
        return _http_error(400, "user_message_index must be >= 0")
    # Refuse rewind while a turn is still streaming for this chat to avoid
    # racing with delta/stream_end events that would re-append the
    # truncated content.
    chat_id = decoded_key.split(":", 1)[1] if ":" in decoded_key else decoded_key
    if websocket_turn_wall_started_at(chat_id) is not None:
        return _http_error(409, "session is currently running")
    transcript_removed = rewind_webui_transcript_to_user(decoded_key, user_message_index)
    session_removed = ctx.deps.session_manager.rewind_to_user_message(
        decoded_key, user_message_index
    )
    # Notify connected clients to refresh their thread view.
    ctx.deps.notify_session_updated(chat_id)
    return _http_json_response({
        "rewound": transcript_removed > 0 or session_removed > 0,
        "transcript_lines_removed": transcript_removed,
        "session_messages_removed": session_removed,
    })
