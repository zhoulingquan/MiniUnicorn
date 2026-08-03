"""Service-level tests for the extracted Weixin media service (Task 18 Step 2).

These tests exercise ``WeixinMediaService`` directly with mocked HTTP and
API clients so they run without a real WeChat backend. They assert:

* download uses ``full_url`` when present, falls back to ``encrypt_query_param``
* download retryable error classification (5xx/timeout → fallback)
* download non-image requires AES key
* download decrypts ciphertext using AES key
* download file extension matches media type
* send_file uses ``upload_full_url`` when present, falls back to ``upload_param``
* send_file encrypts file bytes with AES-128-ECB before CDN upload
* send_file builds correct ``getuploadurl`` and ``sendmessage`` payloads
* send_file uses correct ``media_type`` / ``item_type`` per extension
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from miniunicorn.channels.weixin.api_client import WeixinApiClient
from miniunicorn.channels.weixin.crypto import encrypt_aes_ecb
from miniunicorn.channels.weixin.media import WeixinMediaService

# base64("0123456789abcdef") — 16 raw bytes
_KEY_B64 = "MDEyMzQ1Njc4OWFiY2RlZg=="


class _FakeCdnResponse:
    """Minimal stand-in for an httpx CDN response."""

    def __init__(
        self,
        *,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
        status_code: int = 200,
    ) -> None:
        self.content = content
        self.headers = headers or {}
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://cdn.test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=request, response=response
            )


class _FakeErrorCdnResponse(_FakeCdnResponse):
    def __init__(self, *, url: str, status_code: int) -> None:
        super().__init__(status_code=status_code)
        self._url = url

    def raise_for_status(self) -> None:
        request = httpx.Request("GET", self._url)
        response = httpx.Response(self.status_code, request=request)
        raise httpx.HTTPStatusError(
            f"download failed with status {self.status_code}",
            request=request,
            response=response,
        )


def _make_api_client() -> WeixinApiClient:
    return WeixinApiClient(
        client=httpx.AsyncClient(),
        base_url="https://ilinkai.weixin.qq.com",
        token_getter=lambda: "test-token",
        route_tag=None,
    )


def _make_media_service(
    *,
    cdn_get_mock: AsyncMock | None = None,
    cdn_post_mock: AsyncMock | None = None,
    api_post_mock: AsyncMock | None = None,
    media_dir: Path,
) -> tuple[WeixinMediaService, WeixinApiClient]:
    api_client = _make_api_client()
    if api_post_mock is not None:
        api_client.post = api_post_mock  # type: ignore[method-assign]

    cdn_client = httpx.AsyncClient()
    if cdn_get_mock is not None:
        cdn_client.get = cdn_get_mock  # type: ignore[method-assign]
    if cdn_post_mock is not None:
        cdn_client.post = cdn_post_mock  # type: ignore[method-assign]

    service = WeixinMediaService(
        api_client=api_client,
        cdn_client=cdn_client,
        cdn_base_url="https://novac2c.cdn.weixin.qq.com/c2c",
        media_dir=media_dir,
    )
    return service, api_client


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_uses_full_url_when_present(tmp_path: Path) -> None:
    full_url = "https://cdn.example.test/download/full"
    cdn_get = AsyncMock(return_value=_FakeCdnResponse(content=b"raw-image-bytes"))
    service, _ = _make_media_service(cdn_get_mock=cdn_get, media_dir=tmp_path)

    item = {"media": {"full_url": full_url, "encrypt_query_param": "enc-not-used"}}
    saved = await service.download(item, "image")

    assert saved is not None
    assert Path(saved).read_bytes() == b"raw-image-bytes"
    cdn_get.assert_awaited_once_with(full_url)


@pytest.mark.asyncio
async def test_download_falls_back_to_encrypt_query_param(tmp_path: Path) -> None:
    cdn_get = AsyncMock(return_value=_FakeCdnResponse(content=b"fallback-bytes"))
    service, _ = _make_media_service(cdn_get_mock=cdn_get, media_dir=tmp_path)

    item = {"media": {"encrypt_query_param": "enc-fallback"}}
    saved = await service.download(item, "image")

    assert saved is not None
    assert Path(saved).read_bytes() == b"fallback-bytes"
    called_url = cdn_get.await_args_list[0].args[0]
    assert called_url.startswith(
        "https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param=enc-fallback"
    )


@pytest.mark.asyncio
async def test_download_retries_on_retryable_error_then_falls_back(tmp_path: Path) -> None:
    full_url = "https://cdn.example.test/download/full?taskid=123"
    cdn_get = AsyncMock(
        side_effect=[
            _FakeErrorCdnResponse(url=full_url, status_code=500),
            _FakeCdnResponse(content=b"fallback-bytes"),
        ]
    )
    service, _ = _make_media_service(cdn_get_mock=cdn_get, media_dir=tmp_path)

    item = {"media": {"full_url": full_url, "encrypt_query_param": "enc-fallback"}}
    saved = await service.download(item, "image")

    assert saved is not None
    assert Path(saved).read_bytes() == b"fallback-bytes"
    assert cdn_get.await_count == 2
    assert cdn_get.await_args_list[0].args[0] == full_url
    fallback_url = cdn_get.await_args_list[1].args[0]
    assert "encrypted_query_param=enc-fallback" in fallback_url


@pytest.mark.asyncio
async def test_download_does_not_retry_when_no_fallback(tmp_path: Path) -> None:
    full_url = "https://cdn.example.test/download/full"
    cdn_get = AsyncMock(return_value=_FakeErrorCdnResponse(url=full_url, status_code=500))
    service, _ = _make_media_service(cdn_get_mock=cdn_get, media_dir=tmp_path)

    item = {"media": {"full_url": full_url}}
    saved = await service.download(item, "image")

    assert saved is None
    cdn_get.assert_awaited_once_with(full_url)


@pytest.mark.asyncio
async def test_download_non_image_requires_aes_key(tmp_path: Path) -> None:
    full_url = "https://cdn.example.test/download/voice"
    cdn_get = AsyncMock(return_value=_FakeCdnResponse(content=b"some-bytes"))
    service, _ = _make_media_service(cdn_get_mock=cdn_get, media_dir=tmp_path)

    item = {"media": {"full_url": full_url}}
    saved = await service.download(item, "voice")

    assert saved is None
    cdn_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_decrypts_with_aes_key(tmp_path: Path) -> None:
    plaintext = b"decrypted-image-data"
    ciphertext = encrypt_aes_ecb(plaintext, _KEY_B64)

    full_url = "https://cdn.example.test/download/full"
    cdn_get = AsyncMock(return_value=_FakeCdnResponse(content=ciphertext))
    service, _ = _make_media_service(cdn_get_mock=cdn_get, media_dir=tmp_path)

    # aeskey is hex string of 16 bytes
    aeskey_hex = "30313233343536373839616263646566"
    item = {"aeskey": aeskey_hex, "media": {"full_url": full_url}}
    saved = await service.download(item, "image")

    assert saved is not None
    assert Path(saved).read_bytes() == plaintext


@pytest.mark.asyncio
async def test_download_uses_media_aes_key_when_item_aeskey_absent(tmp_path: Path) -> None:
    plaintext = b"decrypted-data"
    ciphertext = encrypt_aes_ecb(plaintext, _KEY_B64)

    full_url = "https://cdn.example.test/download/full"
    cdn_get = AsyncMock(return_value=_FakeCdnResponse(content=ciphertext))
    service, _ = _make_media_service(cdn_get_mock=cdn_get, media_dir=tmp_path)

    media_aes_key_b64 = _KEY_B64
    item = {"media": {"full_url": full_url, "aes_key": media_aes_key_b64}}
    saved = await service.download(item, "image")

    assert saved is not None
    assert Path(saved).read_bytes() == plaintext


@pytest.mark.asyncio
async def test_download_file_extension_for_image(tmp_path: Path) -> None:
    cdn_get = AsyncMock(return_value=_FakeCdnResponse(content=b"img"))
    service, _ = _make_media_service(cdn_get_mock=cdn_get, media_dir=tmp_path)

    item = {"media": {"encrypt_query_param": "enc-1"}}
    saved = await service.download(item, "image")

    assert saved is not None
    assert Path(saved).suffix == ".jpg"


@pytest.mark.asyncio
async def test_download_file_extension_for_voice(tmp_path: Path) -> None:
    plaintext = b"voice-data-padded!!"  # 18 bytes → will be padded to 32 by encrypt
    ciphertext = encrypt_aes_ecb(plaintext, _KEY_B64)
    cdn_get = AsyncMock(return_value=_FakeCdnResponse(content=ciphertext))
    service, _ = _make_media_service(cdn_get_mock=cdn_get, media_dir=tmp_path)

    item = {"aeskey": "30313233343536373839616263646566", "media": {"encrypt_query_param": "enc"}}
    saved = await service.download(item, "voice")

    assert saved is not None
    assert Path(saved).suffix == ".silk"


@pytest.mark.asyncio
async def test_download_uses_filename_when_provided(tmp_path: Path) -> None:
    # Use image type so AES key is not required.
    cdn_get = AsyncMock(return_value=_FakeCdnResponse(content=b"img-bytes"))
    service, _ = _make_media_service(cdn_get_mock=cdn_get, media_dir=tmp_path)

    item = {"media": {"encrypt_query_param": "enc"}}
    saved = await service.download(item, "image", filename="report.pdf")

    assert saved is not None
    assert Path(saved).name == "report.pdf"


@pytest.mark.asyncio
async def test_download_sanitizes_filename_path_traversal(tmp_path: Path) -> None:
    # Use image type so AES key is not required.
    cdn_get = AsyncMock(return_value=_FakeCdnResponse(content=b"img-bytes"))
    service, _ = _make_media_service(cdn_get_mock=cdn_get, media_dir=tmp_path)

    item = {"media": {"encrypt_query_param": "enc"}}
    saved = await service.download(item, "image", filename="../../../etc/passwd")

    assert saved is not None
    # basename only — no traversal
    assert Path(saved).parent == tmp_path
    assert Path(saved).name == "passwd"


# ---------------------------------------------------------------------------
# send_file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_file_uses_upload_full_url_when_present(tmp_path: Path) -> None:
    media_file = tmp_path / "photo.jpg"
    media_file.write_bytes(b"hello-weixin")

    cdn_post = AsyncMock(return_value=_FakeCdnResponse(headers={"x-encrypted-param": "dl-param"}))
    api_post = AsyncMock(
        side_effect=[
            {
                "upload_full_url": "https://upload-full.example.test/path?foo=bar",
                "upload_param": "x",
            },
            {"ret": 0},
        ]
    )
    service, _ = _make_media_service(
        cdn_post_mock=cdn_post, api_post_mock=api_post, media_dir=tmp_path
    )

    await service.send_file("wx-user", str(media_file), "ctx-1")

    cdn_url = cdn_post.await_args_list[0].args[0]
    assert cdn_url == "https://upload-full.example.test/path?foo=bar"


@pytest.mark.asyncio
async def test_send_file_falls_back_to_upload_param_url(tmp_path: Path) -> None:
    media_file = tmp_path / "photo.jpg"
    media_file.write_bytes(b"hello-weixin")

    cdn_post = AsyncMock(return_value=_FakeCdnResponse(headers={"x-encrypted-param": "dl-param"}))
    api_post = AsyncMock(
        side_effect=[
            {"upload_param": "enc-need-fallback"},
            {"ret": 0},
        ]
    )
    service, _ = _make_media_service(
        cdn_post_mock=cdn_post, api_post_mock=api_post, media_dir=tmp_path
    )

    await service.send_file("wx-user", str(media_file), "ctx-1")

    cdn_url = cdn_post.await_args_list[0].args[0]
    assert cdn_url.startswith(
        "https://novac2c.cdn.weixin.qq.com/c2c/upload?encrypted_query_param=enc-need-fallback"
    )
    assert "&filekey=" in cdn_url


@pytest.mark.asyncio
async def test_send_file_encrypts_bytes_before_upload(tmp_path: Path) -> None:
    raw_data = b"hello-weixin"
    media_file = tmp_path / "photo.jpg"
    media_file.write_bytes(raw_data)

    captured: dict[str, Any] = {}

    def _cdn_post_handler(url: str, *, content: bytes = b"", **kwargs) -> _FakeCdnResponse:
        captured["url"] = url
        captured["content"] = content
        return _FakeCdnResponse(headers={"x-encrypted-param": "dl-param"})

    cdn_post = AsyncMock(side_effect=_cdn_post_handler)
    api_post = AsyncMock(
        side_effect=[
            {"upload_full_url": "https://upload.test/up"},
            {"ret": 0},
        ]
    )
    service, _ = _make_media_service(
        cdn_post_mock=cdn_post, api_post_mock=api_post, media_dir=tmp_path
    )

    await service.send_file("wx-user", str(media_file), "ctx-1")

    # The uploaded bytes must NOT be the raw plaintext — they must be
    # AES-128-ECB encrypted + PKCS7 padded.
    assert captured["content"] != raw_data
    assert len(captured["content"]) % 16 == 0


@pytest.mark.asyncio
async def test_send_file_getuploadurl_payload(tmp_path: Path) -> None:
    raw_data = b"hello"
    media_file = tmp_path / "photo.jpg"
    media_file.write_bytes(raw_data)

    cdn_post = AsyncMock(return_value=_FakeCdnResponse(headers={"x-encrypted-param": "dl-param"}))
    api_post = AsyncMock(
        side_effect=[
            {"upload_full_url": "https://upload.test/up"},
            {"ret": 0},
        ]
    )
    service, _ = _make_media_service(
        cdn_post_mock=cdn_post, api_post_mock=api_post, media_dir=tmp_path
    )

    await service.send_file("wx-user", str(media_file), "ctx-1")

    getupload_body = api_post.await_args_list[0].args[1]
    assert getupload_body["media_type"] == 1  # UPLOAD_MEDIA_IMAGE
    assert getupload_body["to_user_id"] == "wx-user"
    assert getupload_body["rawsize"] == len(raw_data)
    assert "rawfilemd5" in getupload_body
    assert "aeskey" in getupload_body
    assert "filekey" in getupload_body
    # Padded size: ceil((5+1)/16)*16 = 16
    assert getupload_body["filesize"] == 16


@pytest.mark.asyncio
async def test_send_file_sendmessage_payload_for_image(tmp_path: Path) -> None:
    raw_data = b"hello"
    media_file = tmp_path / "photo.jpg"
    media_file.write_bytes(raw_data)

    cdn_post = AsyncMock(return_value=_FakeCdnResponse(headers={"x-encrypted-param": "dl-param"}))
    api_post = AsyncMock(
        side_effect=[
            {"upload_full_url": "https://upload.test/up"},
            {"ret": 0},
        ]
    )
    service, _ = _make_media_service(
        cdn_post_mock=cdn_post, api_post_mock=api_post, media_dir=tmp_path
    )

    await service.send_file("wx-user", str(media_file), "ctx-1")

    sendmessage_body = api_post.await_args_list[1].args[1]
    msg = sendmessage_body["msg"]
    assert msg["to_user_id"] == "wx-user"
    assert msg["context_token"] == "ctx-1"
    assert msg["message_type"] == 2  # MESSAGE_TYPE_BOT
    item = msg["item_list"][0]
    assert item["type"] == 2  # ITEM_IMAGE
    assert "image_item" in item
    media = item["image_item"]["media"]
    assert media["encrypt_query_param"] == "dl-param"
    assert "aes_key" in media
    assert media["encrypt_type"] == 1


@pytest.mark.asyncio
async def test_send_file_voice_uses_voice_item_and_upload_type(tmp_path: Path) -> None:
    media_file = tmp_path / "voice.mp3"
    media_file.write_bytes(b"voice-bytes")

    cdn_post = AsyncMock(return_value=_FakeCdnResponse(headers={"x-encrypted-param": "voice-dl"}))
    api_post = AsyncMock(
        side_effect=[
            {"upload_full_url": "https://upload.test/up"},
            {"ret": 0},
        ]
    )
    service, _ = _make_media_service(
        cdn_post_mock=cdn_post, api_post_mock=api_post, media_dir=tmp_path
    )

    await service.send_file("wx-user", str(media_file), "ctx-voice")

    getupload_body = api_post.await_args_list[0].args[1]
    assert getupload_body["media_type"] == 4  # UPLOAD_MEDIA_VOICE

    sendmessage_body = api_post.await_args_list[1].args[1]
    item = sendmessage_body["msg"]["item_list"][0]
    assert item["type"] == 3  # ITEM_VOICE
    assert "voice_item" in item
    assert "file_item" not in item
    assert item["voice_item"]["media"]["encrypt_query_param"] == "voice-dl"


@pytest.mark.asyncio
async def test_send_file_file_uses_file_item_and_includes_filename(tmp_path: Path) -> None:
    media_file = tmp_path / "doc.pdf"
    media_file.write_bytes(b"pdf-bytes")

    cdn_post = AsyncMock(return_value=_FakeCdnResponse(headers={"x-encrypted-param": "file-dl"}))
    api_post = AsyncMock(
        side_effect=[
            {"upload_full_url": "https://upload.test/up"},
            {"ret": 0},
        ]
    )
    service, _ = _make_media_service(
        cdn_post_mock=cdn_post, api_post_mock=api_post, media_dir=tmp_path
    )

    await service.send_file("wx-user", str(media_file), "ctx-file")

    getupload_body = api_post.await_args_list[0].args[1]
    assert getupload_body["media_type"] == 3  # UPLOAD_MEDIA_FILE

    sendmessage_body = api_post.await_args_list[1].args[1]
    item = sendmessage_body["msg"]["item_list"][0]
    assert item["type"] == 4  # ITEM_FILE
    file_item = item["file_item"]
    assert file_item["file_name"] == "doc.pdf"
    assert file_item["len"] == str(len(b"pdf-bytes"))


@pytest.mark.asyncio
async def test_send_file_video_uses_video_item(tmp_path: Path) -> None:
    media_file = tmp_path / "clip.mp4"
    media_file.write_bytes(b"video-bytes")

    cdn_post = AsyncMock(return_value=_FakeCdnResponse(headers={"x-encrypted-param": "video-dl"}))
    api_post = AsyncMock(
        side_effect=[
            {"upload_full_url": "https://upload.test/up"},
            {"ret": 0},
        ]
    )
    service, _ = _make_media_service(
        cdn_post_mock=cdn_post, api_post_mock=api_post, media_dir=tmp_path
    )

    await service.send_file("wx-user", str(media_file), "ctx-video")

    getupload_body = api_post.await_args_list[0].args[1]
    assert getupload_body["media_type"] == 2  # UPLOAD_MEDIA_VIDEO

    sendmessage_body = api_post.await_args_list[1].args[1]
    item = sendmessage_body["msg"]["item_list"][0]
    assert item["type"] == 5  # ITEM_VIDEO
    assert "video_item" in item


@pytest.mark.asyncio
async def test_send_file_raises_on_missing_upload_url(tmp_path: Path) -> None:
    media_file = tmp_path / "photo.jpg"
    media_file.write_bytes(b"data")

    cdn_post = AsyncMock()
    api_post = AsyncMock(return_value={"upload_full_url": "", "upload_param": ""})
    service, _ = _make_media_service(
        cdn_post_mock=cdn_post, api_post_mock=api_post, media_dir=tmp_path
    )

    with pytest.raises(RuntimeError, match="getuploadurl"):
        await service.send_file("wx-user", str(media_file), "ctx-1")


@pytest.mark.asyncio
async def test_send_file_raises_on_missing_encrypted_param_header(tmp_path: Path) -> None:
    media_file = tmp_path / "photo.jpg"
    media_file.write_bytes(b"data")

    cdn_post = AsyncMock(return_value=_FakeCdnResponse(headers={}))
    api_post = AsyncMock(
        side_effect=[
            {"upload_full_url": "https://upload.test/up"},
            {"ret": 0},
        ]
    )
    service, _ = _make_media_service(
        cdn_post_mock=cdn_post, api_post_mock=api_post, media_dir=tmp_path
    )

    with pytest.raises(RuntimeError, match="x-encrypted-param"):
        await service.send_file("wx-user", str(media_file), "ctx-1")


@pytest.mark.asyncio
async def test_send_file_raises_on_file_not_found(tmp_path: Path) -> None:
    service, _ = _make_media_service(media_dir=tmp_path)

    with pytest.raises(FileNotFoundError):
        await service.send_file("wx-user", str(tmp_path / "nonexistent.jpg"), "ctx-1")
