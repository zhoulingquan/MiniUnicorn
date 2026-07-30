"""Message tool for sending messages to users."""

from contextvars import ContextVar
from pathlib import Path
from typing import Any, Awaitable, Callable

from miniunicorn.agent.tools.base import Tool, tool_parameters
from miniunicorn.agent.tools.context import ContextAware, RequestContext
from miniunicorn.agent.tools.path_utils import resolve_workspace_path
from miniunicorn.agent.tools.schema import ArraySchema, StringSchema, tool_parameters_schema
from miniunicorn.bus.events import OutboundMessage
from miniunicorn.config.paths import get_workspace_path
from miniunicorn.security.workspace_access import current_tool_workspace


@tool_parameters(
    tool_parameters_schema(
        content=StringSchema(
            "Message content for proactive or cross-channel delivery. "
            "Do not use this for a normal reply in the current chat."
        ),
        channel=StringSchema(
            "Optional target channel for cross-channel/proactive delivery. "
            "Do not set this to the current runtime channel for a normal reply."
        ),
        chat_id=StringSchema(
            "Optional target chat/user ID for cross-channel/proactive delivery. "
            "On WebSocket/WebUI turns: omit chat_id to use the server's conversation id "
            "(never pass client_id values like anon-…). "
            "Do not set this to the current runtime chat for a normal reply."
        ),
        media=ArraySchema(
            StringSchema(""),
            description=(
                "Optional list of existing file paths to attach. "
                "Use artifact paths returned by generate_image here when delivering generated images."
            ),
        ),
        buttons=ArraySchema(
            ArraySchema(StringSchema("Button label")),
            description="Optional: inline keyboard buttons as list of rows, each row is list of button labels.",
        ),
        required=["content"],
    )
)
class MessageTool(Tool, ContextAware):
    """Tool to send messages to users on chat channels."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        default_message_id: str | None = None,
        workspace: str | Path | None = None,
        restrict_to_workspace: bool = False,
    ):
        self._send_callback = send_callback
        self._workspace = (
            Path(workspace).expanduser() if workspace is not None else get_workspace_path()
        )
        self._restrict_to_workspace = restrict_to_workspace
        self._default_channel: ContextVar[str] = ContextVar(
            "message_default_channel", default=default_channel
        )
        self._default_chat_id: ContextVar[str] = ContextVar(
            "message_default_chat_id", default=default_chat_id
        )
        self._default_message_id: ContextVar[str | None] = ContextVar(
            "message_default_message_id",
            default=default_message_id,
        )
        self._default_metadata: ContextVar[dict[str, Any]] = ContextVar(
            "message_default_metadata",
            default={},
        )
        self._sent_in_turn_var: ContextVar[bool] = ContextVar("message_sent_in_turn", default=False)
        self._turn_delivered_media_var: ContextVar[tuple[str, ...]] = ContextVar(
            "message_turn_delivered_media",
            default=(),
        )
        self._record_channel_delivery_var: ContextVar[bool] = ContextVar(
            "message_record_channel_delivery",
            default=False,
        )

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        send_callback = ctx.bus.publish_outbound if ctx.bus else None
        return cls(
            send_callback=send_callback,
            workspace=ctx.workspace,
            restrict_to_workspace=ctx.config.restrict_to_workspace,
        )

    def set_context(self, ctx: RequestContext) -> None:
        """Set the current message context."""
        self._default_channel.set(ctx.channel)
        self._default_chat_id.set(ctx.chat_id)
        self._default_message_id.set(ctx.message_id)
        self._default_metadata.set(dict(ctx.metadata or {}))

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback for sending messages."""
        self._send_callback = callback

    def start_turn(self) -> None:
        """Reset per-turn send tracking."""
        self._sent_in_turn = False
        self._turn_delivered_media_var.set(())

    def turn_delivered_media_paths(self) -> list[str]:
        """Absolute paths attached via this tool to the active chat in the current turn."""
        return list(self._turn_delivered_media_var.get())

    def set_record_channel_delivery(self, active: bool):
        """Mark tool-sent messages as proactive channel deliveries."""
        return self._record_channel_delivery_var.set(active)

    def reset_record_channel_delivery(self, token) -> None:
        """Restore previous proactive delivery recording state."""
        self._record_channel_delivery_var.reset(token)

    @property
    def _sent_in_turn(self) -> bool:
        return self._sent_in_turn_var.get()

    @_sent_in_turn.setter
    def _sent_in_turn(self, value: bool) -> None:
        self._sent_in_turn_var.set(value)

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return (
            "Proactively send a message to a user/channel, optionally with file attachments. "
            "Use this for reminders, cross-channel delivery, or explicit proactive sends. "
            "Do not use this for the normal reply in the current chat: answer naturally instead. "
            "If channel/chat_id would target the current runtime conversation, do not call this tool "
            "unless the user explicitly asked you to proactively send an existing file attachment. "
            "When generate_image creates images in the current chat, use the message tool "
            "with the artifact paths in the media parameter to deliver the images to the user. "
            "For proactive attachment delivery, use the 'media' parameter with file paths. "
            "Do NOT use read_file to send files — that only reads content for your own analysis."
        )

    def _resolve_media(self, media: list[str]) -> list[str]:
        """Resolve local media attachments and enforce workspace restriction when enabled."""
        resolved: list[str] = []
        access = current_tool_workspace(
            self._workspace,
            restrict_to_workspace=self._restrict_to_workspace,
        )
        workspace = access.project_path or self._workspace
        for p in media:
            if p.startswith(("http://", "https://")):
                resolved.append(p)
            elif not access.restrict_to_workspace:
                path = Path(p).expanduser()
                resolved.append(p if path.is_absolute() else str(workspace / path))
            else:
                resolved.append(str(resolve_workspace_path(p, workspace, access.allowed_root)))
        return resolved

    def _try_durable_enqueue(
        self,
        *,
        content: str,
        channel: str,
        chat_id: str,
        media: list[str] | None,
        same_target: bool,
        message_id: str | None,
    ) -> str | None:
        """Enqueue to the Outbox when running under the durable runtime.

        Returns the tool result string (with ``outbox_id`` receipt) when
        the durable delivery ledger and active claim are bound, or
        ``None`` when the legacy direct-Channel path should be used
        (design §20.6, WP5 task 5).
        """
        import hashlib

        # Lazy import avoids a hard runtime dependency in the tools layer.
        try:
            from miniunicorn.runtime.session_committer import (
                _get_claim_for_task,
                _get_delivery_ledger_for_task,
                _get_active_tool_call_id,
            )
            from miniunicorn.agent.turn_runtime import current_turn_runtime
        except ImportError:
            return None

        runtime = current_turn_runtime()
        if runtime is None or runtime.task_id is None:
            return None

        task_id = runtime.task_id
        claim = _get_claim_for_task(task_id)
        if claim is None:
            return None

        store = _get_delivery_ledger_for_task(task_id)
        if store is None:
            return None

        tool_call_id = _get_active_tool_call_id(task_id) or ""

        # Build the payload blob (design §20.6 step 2).
        payload_bytes = content.encode("utf-8")
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()
        try:
            from miniunicorn.runtime.models import BlobWrite

            blob = store.write_blob(
                BlobWrite(
                    scope_key=f"task/{task_id}",
                    blob_kind="OUTBOX_PAYLOAD",
                    content_hash=payload_hash,
                    encoding="RAW_BYTES",
                    inline_content=payload_bytes,
                    size_bytes=len(payload_bytes),
                )
            )
            payload_blob_id = blob.blob_id
        except Exception:
            # If blob write fails, fall back to legacy path rather than
            # losing the message entirely.
            return None

        # Stable dedup key (design §16.6): sha256(task_id:tool_call_id:message)
        dedup_key = hashlib.sha256(
            f"{task_id}:{tool_call_id}:message".encode("utf-8")
        ).hexdigest()

        # Enqueue under the task lease (design §20.6 steps 2+3).
        try:
            outbox_id = store.enqueue_message_tool_outbox(
                claim,
                channel=channel,
                channel_account="",
                target_key=chat_id,
                payload_blob_id=payload_blob_id,
                payload_hash=payload_hash,
                dedup_key=dedup_key,
            )
        except Exception:
            return None

        # Mark as sent in turn so the Worker can suppress the final reply
        # when the MessageTool targeted the current chat (design §17.8).
        if same_target:
            self._sent_in_turn = True
            if media:
                prev = self._turn_delivered_media_var.get()
                self._turn_delivered_media_var.set(
                    prev + tuple(str(p) for p in media)
                )

        media_info = f" with {len(media)} attachments" if media else ""
        return (
            f"Message queued for delivery to {channel}:{chat_id}{media_info} "
            f"(outbox_id={outbox_id})"
        )

    async def execute(
        self,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        media: list[str] | None = None,
        buttons: list[list[str]] | None = None,
        **kwargs: Any,
    ) -> str:
        from miniunicorn.utils.helpers import strip_think

        content = strip_think(content)

        if buttons is not None:
            if not isinstance(buttons, list) or any(
                not isinstance(row, list) or any(not isinstance(label, str) for label in row)
                for row in buttons
            ):
                return "Error: buttons must be a list of list of strings"
        default_channel = self._default_channel.get()
        default_chat_id = self._default_chat_id.get()
        channel = channel or default_channel
        explicit_chat_id = chat_id
        if (
            default_channel == "websocket"
            and channel == "websocket"
            and explicit_chat_id is not None
            and str(explicit_chat_id).strip() != ""
            and str(explicit_chat_id).strip() != str(default_chat_id).strip()
        ):
            return (
                "Error: chat_id does not match the active WebSocket conversation. "
                "Omit chat_id (and usually channel) so delivery uses the current "
                "conversation id from context — WebSocket client_id strings "
                "(e.g. anon-…) are not chat ids."
            )
        chat_id = chat_id or default_chat_id
        # Only inherit default message_id when targeting the same channel+chat.
        # Cross-chat sends must not carry the original message_id, because
        # some channels (e.g. Feishu) use it to determine the target
        # conversation via their Reply API, which would route the message
        # to the wrong chat entirely.
        same_target = channel == default_channel and chat_id == default_chat_id
        if same_target:
            message_id = message_id or self._default_message_id.get()
        else:
            message_id = None

        if not channel or not chat_id:
            return "Error: No target channel/chat specified"

        if media:
            try:
                media = self._resolve_media(media)
            except (OSError, PermissionError, ValueError) as e:
                return f"Error: media path is not allowed: {str(e)}"

        # Durable runtime: route through Outbox instead of direct Channel
        # callback (design §20.6, WP5 task 5). When an active delivery
        # ledger and claim are bound, enqueue one Outbox row with a stable
        # dedup key and return the outbox_id as the tool receipt. The
        # OutboxSender performs actual Channel delivery.
        durable_result = self._try_durable_enqueue(
            content=content,
            channel=channel,
            chat_id=chat_id,
            media=media,
            same_target=same_target,
            message_id=message_id,
        )
        if durable_result is not None:
            return durable_result

        if not self._send_callback:
            return "Error: Message sending not configured"

        metadata = dict(self._default_metadata.get()) if same_target else {}
        if message_id:
            metadata["message_id"] = message_id
        if self._record_channel_delivery_var.get() or media:
            metadata["_record_channel_delivery"] = True

        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            media=media or [],
            buttons=buttons or [],
            metadata=metadata,
        )

        try:
            await self._send_callback(msg)
            if channel == default_channel and chat_id == default_chat_id:
                self._sent_in_turn = True
                if media:
                    prev = self._turn_delivered_media_var.get()
                    self._turn_delivered_media_var.set(prev + tuple(str(p) for p in media))
            media_info = f" with {len(media)} attachments" if media else ""
            button_info = f" with {sum(len(row) for row in buttons)} button(s)" if buttons else ""
            return f"Message sent to {channel}:{chat_id}{media_info}{button_info}"
        except Exception as e:
            return f"Error sending message: {str(e)}"
