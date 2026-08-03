"""Feishu/Lark media operations extracted from the channel façade.

``FeishuMediaService`` owns the SDK-bound upload/download logic plus the
download-and-save orchestration and the path-safe filename helper. The
channel delegates to it with unchanged method signatures and retains
ownership of ``asyncio.to_thread`` / ``run_in_executor`` offloading.

Synchronous SDK calls stay synchronous inside this service so the channel
can decide how (and whether) to run them in a worker thread.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from miniunicorn.utils.helpers import safe_filename

# Maps a file extension to the Feishu OpenAPI ``file_type`` enum used by the
# ``CreateFile`` request. Anything not listed falls back to ``"stream"``.
_FILE_TYPE_MAP = {
    ".opus": "opus",
    ".mp4": "mp4",
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "doc",
    ".xls": "xls",
    ".xlsx": "xls",
    ".ppt": "ppt",
    ".pptx": "ppt",
}


class FeishuMediaService:
    """SDK-bound Feishu media upload/download helper.

    The service is constructed with the SDK ``client`` and the on-disk
    ``media_dir`` used to persist downloads. Upload/download helpers keep the
    SDK calls synchronous; callers decide whether to offload them to a thread.
    """

    def __init__(self, *, client: Any, media_dir: Path) -> None:
        self._client = client
        self._media_dir = media_dir

    # ── uploads ──────────────────────────────────────────────────────────────

    def upload_image(self, file_path: str) -> str | None:
        """Upload an image to Feishu and return the ``image_key``."""
        from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody

        try:
            with open(file_path, "rb") as f:
                request = (
                    CreateImageRequest.builder()
                    .request_body(
                        CreateImageRequestBody.builder().image_type("message").image(f).build()
                    )
                    .build()
                )
                response = self._client.im.v1.image.create(request)
                if response.success():
                    image_key = response.data.image_key
                    logger.debug("Uploaded image {}: {}", os.path.basename(file_path), image_key)
                    return image_key
                else:
                    logger.error(
                        "Failed to upload image: code={}, msg={}", response.code, response.msg
                    )
                    return None
        except Exception:
            logger.exception("Error uploading image {}", file_path)
            return None

    def upload_file(self, file_path: str) -> str | None:
        """Upload a file to Feishu and return the ``file_key``."""
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody

        ext = os.path.splitext(file_path)[1].lower()
        file_type = _FILE_TYPE_MAP.get(ext, "stream")
        file_name = os.path.basename(file_path)
        try:
            with open(file_path, "rb") as f:
                request = (
                    CreateFileRequest.builder()
                    .request_body(
                        CreateFileRequestBody.builder()
                        .file_type(file_type)
                        .file_name(file_name)
                        .file(f)
                        .build()
                    )
                    .build()
                )
                response = self._client.im.v1.file.create(request)
                if response.success():
                    file_key = response.data.file_key
                    logger.debug("Uploaded file {}: {}", file_name, file_key)
                    return file_key
                else:
                    logger.error(
                        "Failed to upload file: code={}, msg={}", response.code, response.msg
                    )
                    return None
        except Exception:
            logger.exception("Error uploading file {}", file_path)
            return None

    # ── downloads ────────────────────────────────────────────────────────────

    def download_image(self, message_id: str, image_key: str) -> tuple[bytes | None, str | None]:
        """Download an image from a Feishu message by message_id and image_key."""
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        try:
            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(image_key)
                .type("image")
                .build()
            )
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                # GetMessageResourceRequest returns BytesIO, need to read bytes
                if hasattr(file_data, "read"):
                    file_data = file_data.read()
                return file_data, response.file_name
            else:
                logger.error(
                    "Failed to download image: code={}, msg={}", response.code, response.msg
                )
                return None, None
        except Exception:
            logger.exception("Error downloading image {}", image_key)
            return None, None

    def download_file(
        self, message_id: str, file_key: str, resource_type: str = "file"
    ) -> tuple[bytes | None, str | None]:
        """Download a file/audio/media from a Feishu message by message_id and file_key."""
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        # Feishu resource download API only accepts 'image' or 'file' as type.
        # Both 'audio' and 'media' (video) messages use type='file' for download.
        if resource_type in ("audio", "media"):
            resource_type = "file"

        try:
            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(file_key)
                .type(resource_type)
                .build()
            )
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                if hasattr(file_data, "read"):
                    file_data = file_data.read()
                return file_data, response.file_name
            else:
                logger.error(
                    "Failed to download {}: code={}, msg={}",
                    resource_type,
                    response.code,
                    response.msg,
                )
                return None, None
        except Exception:
            logger.exception("Error downloading {} {}", resource_type, file_key)
            return None, None

    # ── download + persist ───────────────────────────────────────────────────

    def download_and_save(
        self,
        msg_type: str,
        content_json: dict,
        message_id: str | None,
        download_image_fn: Callable[..., tuple[bytes | None, str | None]],
        download_file_fn: Callable[..., tuple[bytes | None, str | None]],
    ) -> tuple[str | None, str]:
        """Download media via the injected callbacks and save it to ``media_dir``.

        The download callbacks are injected (rather than calling
        ``self.download_image`` / ``self.download_file`` directly) so the
        channel's overridable sync helpers remain the single seam tests can
        patch. Returns ``(file_path, content_text)`` — ``file_path`` is ``None``
        when the download failed.
        """
        media_dir = self._media_dir

        data, filename = None, None
        fallback_filename = uuid.uuid4().hex

        if msg_type == "image":
            image_key = content_json.get("image_key")
            if image_key and message_id:
                fallback_filename = f"{image_key[:16]}.jpg"
                data, filename = download_image_fn(message_id, image_key)
                if not filename:
                    filename = fallback_filename

        elif msg_type in ("audio", "file", "media"):
            file_key = content_json.get("file_key")
            if not file_key:
                logger.warning("{} message missing file_key: {}", msg_type, content_json)
                return None, f"[{msg_type}: missing file_key]"
            if not message_id:
                logger.warning("{} message missing message_id", msg_type)
                return None, f"[{msg_type}: missing message_id]"

            fallback_filename = file_key[:16]
            data, filename = download_file_fn(message_id, file_key, msg_type)

            if not data:
                logger.warning("{} download failed: file_key={}", msg_type, file_key)
                return None, f"[{msg_type}: download failed]"

            if not filename:
                filename = fallback_filename

            # Feishu voice messages are opus in OGG container.
            # Use .ogg extension for better Whisper compatibility.
            if msg_type == "audio":
                if not any(filename.endswith(ext) for ext in (".opus", ".ogg", ".oga")):
                    filename = f"{filename}.ogg"

        if data and filename:
            filename = self.safe_media_filename(filename, fallback_filename)
            file_path = media_dir / filename
            file_path.write_bytes(data)
            path_str = str(file_path)
            logger.debug("Downloaded {} to {}", msg_type, path_str)
            return path_str, f"[{msg_type}: {path_str}]"

        return None, f"[{msg_type}: download failed]"

    # ── path safety ──────────────────────────────────────────────────────────

    @staticmethod
    def safe_media_filename(filename: str | None, fallback: str) -> str:
        """Return a local-only filename for downloaded Feishu media."""
        candidate = filename or fallback
        # Feishu/Lark filenames come from message metadata. Treat both POSIX
        # and Windows separators as path boundaries before applying the shared
        # filename sanitizer so downloads cannot escape the channel media dir.
        candidate = os.path.basename(candidate.replace("\\", "/"))
        candidate = safe_filename(candidate)
        if candidate in ("", ".", ".."):
            return safe_filename(fallback) or uuid.uuid4().hex
        return candidate
