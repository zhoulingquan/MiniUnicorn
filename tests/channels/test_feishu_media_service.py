"""Service-level tests for the extracted Feishu media service.

These tests exercise ``FeishuMediaService`` directly with fake SDK clients
and fake ``lark_oapi`` builder modules so they run without the optional
``lark-oapi`` dependency installed. They assert:

* request shapes built by the upload/download helpers,
* the returned keys (``image_key`` / ``file_key``),
* downloaded bytes (raw ``bytes`` and ``BytesIO`` bodies),
* sanitized traversal filenames (path safety),
* extension preservation (including the audio ``.ogg`` rewrite),
* failure-to-``None`` behavior identical to the legacy Channel methods.
"""

from __future__ import annotations

import io
import sys
import types
from pathlib import Path

import pytest

from miniunicorn.channels.feishu.media import FeishuMediaService

# ── Fake lark_oapi builder infrastructure ────────────────────────────────────


class _FakeRequest:
    """A built request that records the setter calls for later inspection."""

    def __init__(self, fields: dict) -> None:
        self.fields = fields


class _FakeBuilder:
    """Chainable builder that records every ``.foo(value)`` call."""

    def __init__(self) -> None:
        self._fields: dict = {}

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def _setter(*args):
            self._fields[name] = args[0] if len(args) == 1 else args
            return self

        return _setter

    def build(self) -> _FakeRequest:
        return _FakeRequest(self._fields)


class _FakeRequestClass:
    """Stand-in for ``CreateImageRequest`` etc. exposing ``.builder()``."""

    @classmethod
    def builder(cls) -> _FakeBuilder:
        return _FakeBuilder()


class _FakeData:
    def __init__(self, image_key: str | None = None, file_key: str | None = None) -> None:
        self.image_key = image_key
        self.file_key = file_key


class _FakeResponse:
    def __init__(
        self,
        *,
        success: bool = True,
        data: _FakeData | None = None,
        file: bytes | io.BytesIO | None = None,
        file_name: str | None = None,
        code: int = 0,
        msg: str = "ok",
    ) -> None:
        self._success = success
        self.data = data
        self.file = file
        self.file_name = file_name
        self.code = code
        self.msg = msg

    def success(self) -> bool:
        return self._success

    def get_log_id(self) -> str:
        return "fake_log_id"


class _FakeImageEndpoint:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    def create(self, request: _FakeRequest) -> _FakeResponse:
        self._client.image_requests.append(request)
        return self._client.image_response


class _FakeFileEndpoint:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    def create(self, request: _FakeRequest) -> _FakeResponse:
        self._client.file_requests.append(request)
        return self._client.file_response


class _FakeResourceEndpoint:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    def get(self, request: _FakeRequest) -> _FakeResponse:
        self._client.resource_requests.append(request)
        return self._client.resource_response


class _FakeClient:
    """Fake SDK client recording every media call."""

    def __init__(self) -> None:
        self.image_requests: list[_FakeRequest] = []
        self.file_requests: list[_FakeRequest] = []
        self.resource_requests: list[_FakeRequest] = []
        self.image_response: _FakeResponse = _FakeResponse(
            success=True, data=_FakeData(image_key="img_key_123")
        )
        self.file_response: _FakeResponse = _FakeResponse(
            success=True, data=_FakeData(file_key="file_key_456")
        )
        self.resource_response: _FakeResponse = _FakeResponse(
            success=True, file=b"raw-bytes", file_name="downloaded.bin"
        )
        self.im = types.SimpleNamespace(
            v1=types.SimpleNamespace(
                image=_FakeImageEndpoint(self),
                file=_FakeFileEndpoint(self),
                message_resource=_FakeResourceEndpoint(self),
            )
        )


_LARK_MODULES = [
    "lark_oapi",
    "lark_oapi.api",
    "lark_oapi.api.im",
    "lark_oapi.api.im.v1",
]


@pytest.fixture
def fake_lark_oapi():
    """Install fake ``lark_oapi.api.im.v1`` builder classes into sys.modules."""
    saved = {name: sys.modules.get(name) for name in _LARK_MODULES}
    im_v1 = types.ModuleType("lark_oapi.api.im.v1")
    im_v1.CreateImageRequest = _FakeRequestClass
    im_v1.CreateImageRequestBody = _FakeRequestClass
    im_v1.CreateFileRequest = _FakeRequestClass
    im_v1.CreateFileRequestBody = _FakeRequestClass
    im_v1.GetMessageResourceRequest = _FakeRequestClass
    sys.modules["lark_oapi"] = types.ModuleType("lark_oapi")
    sys.modules["lark_oapi.api"] = types.ModuleType("lark_oapi.api")
    sys.modules["lark_oapi.api.im"] = types.ModuleType("lark_oapi.api.im")
    sys.modules["lark_oapi.api.im.v1"] = im_v1
    try:
        yield im_v1
    finally:
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


# ── upload_image ─────────────────────────────────────────────────────────────


def test_upload_image_returns_image_key_and_request_shape(fake_lark_oapi, tmp_path: Path) -> None:
    client = _FakeClient()
    client.image_response = _FakeResponse(success=True, data=_FakeData(image_key="img_KEY"))
    service = FeishuMediaService(client=client, media_dir=tmp_path)

    image_file = tmp_path / "pic.png"
    image_file.write_bytes(b"\x89PNG\r\n\x1a\n")

    result = service.upload_image(str(image_file))

    assert result == "img_KEY"
    assert len(client.image_requests) == 1
    body = client.image_requests[0].fields["request_body"]
    assert body.fields["image_type"] == "message"
    # image() receives the open binary file handle
    assert hasattr(body.fields["image"], "read")


def test_upload_image_failure_returns_none(fake_lark_oapi, tmp_path: Path) -> None:
    client = _FakeClient()
    client.image_response = _FakeResponse(success=False, code=991000, msg="bad image")
    service = FeishuMediaService(client=client, media_dir=tmp_path)

    image_file = tmp_path / "pic.png"
    image_file.write_bytes(b"data")

    assert service.upload_image(str(image_file)) is None


def test_upload_image_exception_returns_none(fake_lark_oapi, tmp_path: Path) -> None:
    client = _FakeClient()

    def _boom(_request):
        raise RuntimeError("network down")

    client.im.v1.image.create = _boom  # type: ignore[method-assign]
    service = FeishuMediaService(client=client, media_dir=tmp_path)

    image_file = tmp_path / "pic.png"
    image_file.write_bytes(b"data")

    assert service.upload_image(str(image_file)) is None


# ── upload_file ──────────────────────────────────────────────────────────────


def test_upload_file_returns_file_key_and_request_shape(fake_lark_oapi, tmp_path: Path) -> None:
    client = _FakeClient()
    client.file_response = _FakeResponse(success=True, data=_FakeData(file_key="file_KEY"))
    service = FeishuMediaService(client=client, media_dir=tmp_path)

    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"pdf-data")

    result = service.upload_file(str(doc))

    assert result == "file_KEY"
    assert len(client.file_requests) == 1
    body = client.file_requests[0].fields["request_body"]
    assert body.fields["file_type"] == "pdf"
    assert body.fields["file_name"] == "report.pdf"
    assert hasattr(body.fields["file"], "read")


@pytest.mark.parametrize(
    "ext,expected_type",
    [
        (".opus", "opus"),
        (".mp4", "mp4"),
        (".pdf", "pdf"),
        (".doc", "doc"),
        (".docx", "doc"),
        (".xls", "xls"),
        (".xlsx", "xls"),
        (".ppt", "ppt"),
        (".pptx", "ppt"),
        (".zip", "stream"),
    ],
)
def test_upload_file_type_mapping(
    fake_lark_oapi, tmp_path: Path, ext: str, expected_type: str
) -> None:
    client = _FakeClient()
    service = FeishuMediaService(client=client, media_dir=tmp_path)

    doc = tmp_path / f"thing{ext}"
    doc.write_bytes(b"x")

    service.upload_file(str(doc))

    body = client.file_requests[0].fields["request_body"]
    assert body.fields["file_type"] == expected_type


def test_upload_file_failure_returns_none(fake_lark_oapi, tmp_path: Path) -> None:
    client = _FakeClient()
    client.file_response = _FakeResponse(success=False, code=991001, msg="bad file")
    service = FeishuMediaService(client=client, media_dir=tmp_path)

    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"x")

    assert service.upload_file(str(doc)) is None


def test_upload_file_exception_returns_none(fake_lark_oapi, tmp_path: Path) -> None:
    client = _FakeClient()
    client.im.v1.file.create = lambda _r: (_ for _ in ()).throw(OSError("disk"))  # type: ignore[method-assign]
    service = FeishuMediaService(client=client, media_dir=tmp_path)

    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"x")

    assert service.upload_file(str(doc)) is None


# ── download_image ───────────────────────────────────────────────────────────


def test_download_image_returns_bytes_and_filename(fake_lark_oapi, tmp_path: Path) -> None:
    client = _FakeClient()
    client.resource_response = _FakeResponse(
        success=True, file=b"image-bytes", file_name="photo.jpg"
    )
    service = FeishuMediaService(client=client, media_dir=tmp_path)

    data, name = service.download_image("om_msg", "img_key")

    assert data == b"image-bytes"
    assert name == "photo.jpg"
    assert len(client.resource_requests) == 1
    req = client.resource_requests[0]
    assert req.fields["message_id"] == "om_msg"
    assert req.fields["file_key"] == "img_key"
    assert req.fields["type"] == "image"


def test_download_image_reads_bytesio_body(fake_lark_oapi, tmp_path: Path) -> None:
    client = _FakeClient()
    client.resource_response = _FakeResponse(
        success=True, file=io.BytesIO(b"streamed"), file_name="pic.png"
    )
    service = FeishuMediaService(client=client, media_dir=tmp_path)

    data, _name = service.download_image("om_msg", "img_key")

    assert data == b"streamed"


def test_download_image_failure_returns_none_tuple(fake_lark_oapi, tmp_path: Path) -> None:
    client = _FakeClient()
    client.resource_response = _FakeResponse(success=False, code=992001, msg="missing")
    service = FeishuMediaService(client=client, media_dir=tmp_path)

    assert service.download_image("om_msg", "img_key") == (None, None)


def test_download_image_exception_returns_none_tuple(fake_lark_oapi, tmp_path: Path) -> None:
    client = _FakeClient()
    client.im.v1.message_resource.get = lambda _r: (_ for _ in ()).throw(ValueError("boom"))  # type: ignore[method-assign]
    service = FeishuMediaService(client=client, media_dir=tmp_path)

    assert service.download_image("om_msg", "img_key") == (None, None)


# ── download_file ────────────────────────────────────────────────────────────


def test_download_file_returns_bytes_and_filename(fake_lark_oapi, tmp_path: Path) -> None:
    client = _FakeClient()
    client.resource_response = _FakeResponse(success=True, file=b"file-bytes", file_name="doc.pdf")
    service = FeishuMediaService(client=client, media_dir=tmp_path)

    data, name = service.download_file("om_msg", "file_key", "file")

    assert data == b"file-bytes"
    assert name == "doc.pdf"
    req = client.resource_requests[0]
    assert req.fields["type"] == "file"


def test_download_file_normalizes_audio_and_media_to_file(fake_lark_oapi, tmp_path: Path) -> None:
    client = _FakeClient()
    service = FeishuMediaService(client=client, media_dir=tmp_path)

    service.download_file("om_msg", "fk", "audio")
    service.download_file("om_msg", "fk", "media")

    assert client.resource_requests[0].fields["type"] == "file"
    assert client.resource_requests[1].fields["type"] == "file"


def test_download_file_failure_returns_none_tuple(fake_lark_oapi, tmp_path: Path) -> None:
    client = _FakeClient()
    client.resource_response = _FakeResponse(success=False)
    service = FeishuMediaService(client=client, media_dir=tmp_path)

    assert service.download_file("om_msg", "fk", "file") == (None, None)


def test_download_file_exception_returns_none_tuple(fake_lark_oapi, tmp_path: Path) -> None:
    client = _FakeClient()
    client.im.v1.message_resource.get = lambda _r: (_ for _ in ()).throw(KeyError("x"))  # type: ignore[method-assign]
    service = FeishuMediaService(client=client, media_dir=tmp_path)

    assert service.download_file("om_msg", "fk", "file") == (None, None)


# ── safe_media_filename (path safety + extension preservation) ───────────────


def test_safe_media_filename_plain_name_preserved() -> None:
    assert FeishuMediaService.safe_media_filename("photo.jpg", "fb.bin") == "photo.jpg"


def test_safe_media_filename_posix_traversal_stripped() -> None:
    assert FeishuMediaService.safe_media_filename("../../etc/passwd", "fb.bin") == "passwd"


def test_safe_media_filename_windows_traversal_stripped() -> None:
    assert FeishuMediaService.safe_media_filename("..\\..\\evil.exe", "fb.bin") == "evil.exe"


def test_safe_media_filename_none_uses_fallback() -> None:
    assert FeishuMediaService.safe_media_filename(None, "fallback.ogg") == "fallback.ogg"


def test_safe_media_filename_empty_uses_fallback() -> None:
    assert FeishuMediaService.safe_media_filename("", "fallback.ogg") == "fallback.ogg"


def test_safe_media_filename_unsafe_chars_replaced() -> None:
    # ``:`` and ``?`` are unsafe path characters → replaced with ``_``
    assert FeishuMediaService.safe_media_filename("a:b?d.txt", "fb") == "a_b_d.txt"


def test_safe_media_filename_preserves_extension() -> None:
    assert FeishuMediaService.safe_media_filename("data.pdf", "fb") == "data.pdf"
    assert FeishuMediaService.safe_media_filename("song.opus", "fb") == "song.opus"


# ── download_and_save (download bytes, path safety, extension, failure→None) ─


def _no_image(*_args):
    return None, None


def test_download_and_save_writes_bytes_and_returns_path(tmp_path: Path) -> None:
    service = FeishuMediaService(client=None, media_dir=tmp_path)

    def fake_download_file(_mid, _fk, _rt):
        return b"file-bytes", "report.pdf"

    path, content = service.download_and_save(
        "file", {"file_key": "fk"}, "om_msg", _no_image, fake_download_file
    )

    saved = tmp_path / "report.pdf"
    assert path == str(saved)
    assert saved.read_bytes() == b"file-bytes"
    assert content == f"[file: {saved}]"


def test_download_and_save_strips_traversal_filename(tmp_path: Path) -> None:
    service = FeishuMediaService(client=None, media_dir=tmp_path)

    def fake_download_file(_mid, _fk, _rt):
        return b"owned", "../../../escaped.txt"

    path, _content = service.download_and_save(
        "file", {"file_key": "fk"}, "om_msg", _no_image, fake_download_file
    )

    saved = tmp_path / "escaped.txt"
    assert path == str(saved)
    assert saved.read_bytes() == b"owned"
    # Nothing escaped the media dir.
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_download_and_save_preserves_extension(tmp_path: Path) -> None:
    service = FeishuMediaService(client=None, media_dir=tmp_path)

    def fake_download_file(_mid, _fk, _rt):
        return b"data", "doc.pdf"

    path, _content = service.download_and_save(
        "file", {"file_key": "fk"}, "om_msg", _no_image, fake_download_file
    )

    assert path == str(tmp_path / "doc.pdf")


def test_download_and_save_audio_adds_ogg_extension(tmp_path: Path) -> None:
    service = FeishuMediaService(client=None, media_dir=tmp_path)

    def fake_download_file(_mid, _fk, _rt):
        return b"voice", None  # no filename → fallback (file_key prefix)

    path, content = service.download_and_save(
        "audio", {"file_key": "voice_key"}, "om_msg", _no_image, fake_download_file
    )

    saved = tmp_path / "voice_key.ogg"
    assert path == str(saved)
    assert saved.read_bytes() == b"voice"
    assert content == f"[audio: {saved}]"


def test_download_and_save_audio_keeps_existing_ogg_extension(
    tmp_path: Path,
) -> None:
    service = FeishuMediaService(client=None, media_dir=tmp_path)

    def fake_download_file(_mid, _fk, _rt):
        return b"voice", "clip.ogg"

    path, _content = service.download_and_save(
        "audio", {"file_key": "vk"}, "om_msg", _no_image, fake_download_file
    )

    assert path == str(tmp_path / "clip.ogg")


def test_download_and_save_image_writes_bytes(tmp_path: Path) -> None:
    service = FeishuMediaService(client=None, media_dir=tmp_path)

    def fake_download_image(_mid, _ik):
        return b"png-bytes", "shot.png"

    path, content = service.download_and_save(
        "image", {"image_key": "img_key_1234567890"}, "om_msg", fake_download_image, _no_image
    )

    saved = tmp_path / "shot.png"
    assert path == str(saved)
    assert saved.read_bytes() == b"png-bytes"
    assert content == f"[image: {saved}]"


def test_download_and_save_image_missing_message_id_returns_none(
    tmp_path: Path,
) -> None:
    service = FeishuMediaService(client=None, media_dir=tmp_path)

    path, content = service.download_and_save(
        "image", {"image_key": "ik"}, None, _no_image, _no_image
    )

    assert path is None
    assert content == "[image: download failed]"


def test_download_and_save_file_missing_file_key_returns_none(
    tmp_path: Path,
) -> None:
    service = FeishuMediaService(client=None, media_dir=tmp_path)

    path, content = service.download_and_save("file", {}, "om_msg", _no_image, _no_image)

    assert path is None
    assert content == "[file: missing file_key]"


def test_download_and_save_file_missing_message_id_returns_none(
    tmp_path: Path,
) -> None:
    service = FeishuMediaService(client=None, media_dir=tmp_path)

    path, content = service.download_and_save(
        "file", {"file_key": "fk"}, None, _no_image, _no_image
    )

    assert path is None
    assert content == "[file: missing message_id]"


def test_download_and_save_download_failure_returns_none(tmp_path: Path) -> None:
    service = FeishuMediaService(client=None, media_dir=tmp_path)

    def fake_download_file(_mid, _fk, _rt):
        return None, None

    path, content = service.download_and_save(
        "file", {"file_key": "fk"}, "om_msg", _no_image, fake_download_file
    )

    assert path is None
    assert content == "[file: download failed]"
