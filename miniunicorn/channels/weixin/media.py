"""Weixin media upload/download service extracted from the channel façade.

``WeixinMediaService`` owns:

* download + AES-decrypt of inbound media items (images, voice, files, video)
* upload + AES-encrypt of outbound media files to the WeChat CDN
* the ``getuploadurl`` / ``sendmessage`` API orchestration for uploads
* path-safe filename handling for downloaded files

It does NOT own polling, login state, typing state, or message dispatch —
those remain on ``WeixinChannel``. The channel delegates
``_download_media_item`` and ``_send_media_file`` to this service.
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger

from miniunicorn.channels._media_common import _AUDIO_EXTS as _BASE_AUDIO_EXTS
from miniunicorn.channels._media_common import _IMAGE_EXTS
from miniunicorn.channels._media_common import _VIDEO_EXTS as _BASE_VIDEO_EXTS
from miniunicorn.channels.weixin.api_client import (
    BASE_INFO,
    ITEM_FILE,
    ITEM_IMAGE,
    ITEM_VIDEO,
    ITEM_VOICE,
    MESSAGE_STATE_FINISH,
    MESSAGE_TYPE_BOT,
    UPLOAD_MEDIA_FILE,
    UPLOAD_MEDIA_IMAGE,
    UPLOAD_MEDIA_VIDEO,
    UPLOAD_MEDIA_VOICE,
    WeixinApiClient,
)
from miniunicorn.channels.weixin.crypto import (
    decrypt_aes_ecb,
    encrypt_aes_ecb,
)

_VIDEO_EXTS = _BASE_VIDEO_EXTS | {".flv"}
_VOICE_EXTS = _BASE_AUDIO_EXTS | {".silk", ".flac"}


def _ext_for_type(media_type: str) -> str:
    return {
        "image": ".jpg",
        "voice": ".silk",
        "video": ".mp4",
        "file": "",
    }.get(media_type, "")


def _has_downloadable_media_locator(media: dict[str, Any] | None) -> bool:
    if not isinstance(media, dict):
        return False
    return bool(
        str(media.get("encrypt_query_param", "") or "")
        or str(media.get("full_url", "") or "").strip()
    )


class WeixinMediaService:
    """Weixin media upload/download helper.

    Constructed with:

    * ``api_client`` — the ``WeixinApiClient`` for ``getuploadurl`` and
      ``sendmessage`` API calls.
    * ``cdn_client`` — the ``httpx.AsyncClient`` used for CDN download/upload.
    * ``cdn_base_url`` — the CDN base URL for fallback download/upload URLs.
    * ``media_dir`` — the on-disk directory for persisting downloads.
    """

    def __init__(
        self,
        *,
        api_client: WeixinApiClient,
        cdn_client: httpx.AsyncClient,
        cdn_base_url: str,
        media_dir: Path,
    ) -> None:
        self._api_client = api_client
        self._cdn_client = cdn_client
        self._cdn_base_url = cdn_base_url
        self._media_dir = media_dir

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    async def download(
        self,
        typed_item: dict,
        media_type: str,
        filename: str | None = None,
    ) -> str | None:
        """Download + AES-decrypt a media item. Returns local path or None."""
        try:
            media = typed_item.get("media") or {}
            encrypt_query_param = str(media.get("encrypt_query_param", "") or "")
            full_url = str(media.get("full_url", "") or "").strip()

            if not encrypt_query_param and not full_url:
                return None

            # Resolve AES key (media-download.ts:43-45, pic-decrypt.ts:40-52)
            # image_item.aeskey is a raw hex string (16 bytes as 32 hex chars).
            # media.aes_key is always base64-encoded.
            # For images, prefer image_item.aeskey; for others use media.aes_key.
            raw_aeskey_hex = typed_item.get("aeskey", "")
            media_aes_key_b64 = media.get("aes_key", "")

            aes_key_b64: str = ""
            if raw_aeskey_hex:
                # Convert hex → raw bytes → base64 (matches media-download.ts:43-44)
                aes_key_b64 = base64.b64encode(bytes.fromhex(raw_aeskey_hex)).decode()
            elif media_aes_key_b64:
                aes_key_b64 = media_aes_key_b64

            # Reference protocol behavior: VOICE/FILE/VIDEO require aes_key;
            # only IMAGE may be downloaded as plain bytes when key is missing.
            if media_type != "image" and not aes_key_b64:
                return None

            fallback_url = ""
            if encrypt_query_param:
                fallback_url = (
                    f"{self._cdn_base_url}/download"
                    f"?encrypted_query_param={quote(encrypt_query_param)}"
                )

            download_candidates: list[tuple[str, str]] = []
            if full_url:
                download_candidates.append(("full_url", full_url))
            if fallback_url and (not full_url or fallback_url != full_url):
                download_candidates.append(("encrypt_query_param", fallback_url))

            data = b""
            for idx, (download_source, cdn_url) in enumerate(download_candidates):
                try:
                    resp = await self._cdn_client.get(cdn_url)
                    resp.raise_for_status()
                    data = resp.content
                    break
                except Exception as e:
                    has_more_candidates = idx + 1 < len(download_candidates)
                    should_fallback = (
                        download_source == "full_url"
                        and has_more_candidates
                        and self.is_retryable_download_error(e)
                    )
                    if should_fallback:
                        logger.warning(
                            "media download failed via full_url, falling back to "
                            "encrypt_query_param: type={} err={}",
                            media_type,
                            e,
                        )
                        continue
                    raise

            if aes_key_b64 and data:
                data = decrypt_aes_ecb(data, aes_key_b64)

            if not data:
                return None

            ext = _ext_for_type(media_type)
            if not filename:
                ts = int(time.time())
                hash_seed = encrypt_query_param or full_url
                h = abs(hash(hash_seed)) % 100000
                filename = f"{media_type}_{ts}_{h}{ext}"
            safe_name = os.path.basename(filename)
            file_path = self._media_dir / safe_name
            file_path.write_bytes(data)
            return str(file_path)

        except Exception:
            logger.exception("Error downloading media")
            return None

    # ------------------------------------------------------------------
    # Upload + send
    # ------------------------------------------------------------------

    async def send_file(
        self,
        to_user_id: str,
        media_path: str,
        context_token: str,
    ) -> None:
        """Upload a local file to WeChat CDN and send it as a media message.

        Follows the exact protocol from ``@tencent-weixin/openclaw-weixin`` v1.0.3:

        1. Generate a random 16-byte AES key (client-side).
        2. Call ``getuploadurl`` with file metadata + hex-encoded AES key.
        3. AES-128-ECB encrypt the file and POST to CDN (``{cdnBaseUrl}/upload``).
        4. Read ``x-encrypted-param`` header from CDN response as the download param.
        5. Send a ``sendmessage`` with the appropriate media item referencing the upload.
        """
        p = Path(media_path)
        if not p.is_file():
            raise FileNotFoundError(f"Media file not found: {media_path}")

        raw_data = p.read_bytes()
        raw_size = len(raw_data)
        raw_md5 = hashlib.md5(raw_data).hexdigest()

        # Determine upload media type from extension
        ext = p.suffix.lower()
        if ext in _IMAGE_EXTS:
            upload_type = UPLOAD_MEDIA_IMAGE
            item_type = ITEM_IMAGE
            item_key = "image_item"
        elif ext in _VIDEO_EXTS:
            upload_type = UPLOAD_MEDIA_VIDEO
            item_type = ITEM_VIDEO
            item_key = "video_item"
        elif ext in _VOICE_EXTS:
            upload_type = UPLOAD_MEDIA_VOICE
            item_type = ITEM_VOICE
            item_key = "voice_item"
        else:
            upload_type = UPLOAD_MEDIA_FILE
            item_type = ITEM_FILE
            item_key = "file_item"

        # Generate client-side AES-128 key (16 random bytes)
        aes_key_raw = os.urandom(16)
        aes_key_hex = aes_key_raw.hex()

        # Compute encrypted size: PKCS7 padding to 16-byte boundary
        # Matches aesEcbPaddedSize: Math.ceil((size + 1) / 16) * 16
        padded_size = ((raw_size + 1 + 15) // 16) * 16

        # Step 1: Get upload URL from server (prefer upload_full_url, fallback to upload_param)
        file_key = os.urandom(16).hex()
        upload_body: dict[str, Any] = {
            "filekey": file_key,
            "media_type": upload_type,
            "to_user_id": to_user_id,
            "rawsize": raw_size,
            "rawfilemd5": raw_md5,
            "filesize": padded_size,
            "no_need_thumb": True,
            "aeskey": aes_key_hex,
        }

        upload_resp = await self._api_client.post("ilink/bot/getuploadurl", upload_body)

        upload_full_url = str(upload_resp.get("upload_full_url", "") or "").strip()
        upload_param = str(upload_resp.get("upload_param", "") or "")
        if not upload_full_url and not upload_param:
            raise RuntimeError(
                "getuploadurl returned no upload URL "
                f"(need upload_full_url or upload_param): {upload_resp}"
            )

        # Step 2: AES-128-ECB encrypt and POST to CDN
        aes_key_b64 = base64.b64encode(aes_key_raw).decode()
        encrypted_data = encrypt_aes_ecb(raw_data, aes_key_b64)

        if upload_full_url:
            cdn_upload_url = upload_full_url
        else:
            cdn_upload_url = (
                f"{self._cdn_base_url}/upload"
                f"?encrypted_query_param={quote(upload_param)}"
                f"&filekey={quote(file_key)}"
            )

        cdn_resp = await self._cdn_client.post(
            cdn_upload_url,
            content=encrypted_data,
            headers={"Content-Type": "application/octet-stream"},
        )
        cdn_resp.raise_for_status()

        # The download encrypted_query_param comes from CDN response header
        download_param = cdn_resp.headers.get("x-encrypted-param", "")
        if not download_param:
            raise RuntimeError(
                "CDN upload response missing x-encrypted-param header; "
                f"status={cdn_resp.status_code} headers={dict(cdn_resp.headers)}"
            )

        # Step 3: Send message with the media item
        # aes_key for CDNMedia is the hex key encoded as base64
        # (matches: Buffer.from(uploaded.aeskey).toString("base64"))
        cdn_aes_key_b64 = base64.b64encode(aes_key_hex.encode()).decode()

        media_item: dict[str, Any] = {
            "media": {
                "encrypt_query_param": download_param,
                "aes_key": cdn_aes_key_b64,
                "encrypt_type": 1,
            },
        }

        if item_type == ITEM_IMAGE:
            media_item["mid_size"] = padded_size
        elif item_type == ITEM_VIDEO:
            media_item["video_size"] = padded_size
        elif item_type == ITEM_FILE:
            media_item["file_name"] = p.name
            media_item["len"] = str(raw_size)

        # Send each media item as its own message (matching reference plugin)
        client_id = f"miniunicorn-{uuid.uuid4().hex[:12]}"
        item_list: list[dict] = [{"type": item_type, item_key: media_item}]

        weixin_msg: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": MESSAGE_TYPE_BOT,
            "message_state": MESSAGE_STATE_FINISH,
            "item_list": item_list,
        }
        if context_token:
            weixin_msg["context_token"] = context_token

        body: dict[str, Any] = {
            "msg": weixin_msg,
            "base_info": BASE_INFO,
        }

        data = await self._api_client.post("ilink/bot/sendmessage", body)
        ret = data.get("ret", 0)
        errcode = data.get("errcode", 0)
        if (ret is not None and ret != 0) or (errcode is not None and errcode != 0):
            raise RuntimeError(
                f"WeChat send media error (ret={ret}, errcode={errcode}): {data.get('errmsg', '')}"
            )

    # ------------------------------------------------------------------
    # Error classification
    # ------------------------------------------------------------------

    @staticmethod
    def is_retryable_download_error(err: Exception) -> bool:
        """Classify whether a CDN download error is retryable."""
        if isinstance(err, httpx.TimeoutException | httpx.TransportError):
            return True
        if isinstance(err, httpx.HTTPStatusError):
            status_code = err.response.status_code if err.response is not None else 0
            return status_code >= 500
        return False
