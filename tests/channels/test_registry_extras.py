"""Extras split: lazy adapter imports and missing-dependency startup errors.

The five IM adapters (feishu/weixin/dingtalk/qq/wecom) ship as optional
extras. These tests simulate a slim install (monkeypatching imports) and
verify that:

- ``discover_enabled`` raises ``ChannelDependencyError`` with an actionable
  ``pip install miniunicorn-ai[<extra>]`` hint for enabled-but-missing channels
  (both via the ``find_spec`` preflight and via real import failures);
- ``discover_all`` (CLI/WebUI enumeration) keeps skipping instead of raising;
- ``ChannelManager._init_channels`` logs the error loudly yet still loads the
  remaining healthy channels.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from miniunicorn.bus.events import OutboundMessage
from miniunicorn.bus.queue import MessageBus
from miniunicorn.channels.base import BaseChannel
from miniunicorn.channels.registry import (
    ChannelDependencyError,
    discover_all,
    discover_enabled,
    install_hint,
)
from miniunicorn.config.schema import ChannelsConfig

_EP_TARGET = "importlib.metadata.entry_points"
_REGISTRY_IMPORTLIB = "miniunicorn.channels.registry.importlib"


def _broken_import_module(qualname: str):
    raise ImportError(f"No module named 'lark_oapi' (importing {qualname})")


def _make_fake_importlib(import_module=None, find_spec=None):
    """Build a stand-in for the ``importlib`` module used inside registry.

    Defaults simulate a slim install where every required module is absent.
    """
    return SimpleNamespace(
        import_module=import_module or _broken_import_module,
        util=SimpleNamespace(find_spec=find_spec or (lambda name: None)),
    )


# ---------------------------------------------------------------------------
# install_hint mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module_name", "expected"),
    [
        ("feishu", "pip install miniunicorn-ai[feishu]"),
        ("weixin", "pip install miniunicorn-ai[weixin]"),
        ("dingtalk", "pip install miniunicorn-ai[dingtalk]"),
        ("qq", "pip install miniunicorn-ai[qq]"),
        ("wecom", "pip install miniunicorn-ai[wecom]"),
    ],
)
def test_install_hint_maps_builtin_channels_to_matching_extra(module_name, expected):
    assert install_hint(module_name) == expected


def test_install_hint_falls_back_to_channel_name_for_unknown_module():
    assert install_hint("somefuture") == "pip install miniunicorn-ai[somefuture]"


# ---------------------------------------------------------------------------
# discover_enabled strict mode (startup path)
# ---------------------------------------------------------------------------


def test_discover_enabled_raises_with_install_hint_when_sdk_absent(monkeypatch):
    """Preflight: enabled channel whose extra SDK is absent → clear error."""
    monkeypatch.setattr(_REGISTRY_IMPORTLIB, _make_fake_importlib())

    with patch(_EP_TARGET, return_value=[]):
        with pytest.raises(ChannelDependencyError) as excinfo:
            discover_enabled({"feishu"}, _names=["feishu"])

    msg = str(excinfo.value)
    assert "lark_oapi" in msg
    assert "pip install miniunicorn-ai[feishu]" in msg


def test_discover_enabled_raises_when_module_import_fails(monkeypatch):
    """Import failure path (e.g. qq's top-level aiohttp) also gets the hint."""
    seen: list[str] = []

    def _recording_import(qualname: str):
        seen.append(qualname)
        return _broken_import_module(qualname)

    def _present(name: str):
        return object()  # preflight passes; failure happens at import time

    monkeypatch.setattr(
        _REGISTRY_IMPORTLIB, _make_fake_importlib(import_module=_recording_import, find_spec=_present)
    )

    with patch(_EP_TARGET, return_value=[]):
        with pytest.raises(ChannelDependencyError) as excinfo:
            discover_enabled({"qq"}, _names=["qq"])

    assert seen, "adapter module must actually be imported when preflight passes"
    assert "pip install miniunicorn-ai[qq]" in str(excinfo.value)


def test_discover_enabled_does_not_probe_disabled_adapters(monkeypatch):
    """Slim core: disabled/unconfigured adapters are neither imported nor probed."""
    imported: list[str] = []
    probed: list[str] = []

    def _recording_import(qualname: str):
        imported.append(qualname)
        return _broken_import_module(qualname)

    def _recording_find_spec(name: str):
        probed.append(name)
        return None

    monkeypatch.setattr(
        _REGISTRY_IMPORTLIB,
        _make_fake_importlib(import_module=_recording_import, find_spec=_recording_find_spec),
    )

    with patch(_EP_TARGET, return_value=[]):
        with pytest.raises(ChannelDependencyError):
            discover_enabled({"feishu"}, _names=["feishu", "weixin", "dingtalk", "qq", "wecom"])

    assert all("feishu" in qualname for qualname in imported), (
        f"only the enabled channel may be imported, got: {imported}"
    )
    # Only feishu's required SDK ("lark_oapi") may be probed — disabled
    # channels are never touched.
    assert probed.count("lark_oapi") >= 1 and set(probed) <= {"lark_oapi"}, (
        f"unexpected modules probed: {probed}"
    )


# ---------------------------------------------------------------------------
# discover_all non-strict mode (CLI/WebUI enumeration path)
# ---------------------------------------------------------------------------


def test_discover_all_skips_missing_dependencies_without_raising(monkeypatch):
    monkeypatch.setattr(_REGISTRY_IMPORTLIB, _make_fake_importlib())

    with patch(_EP_TARGET, return_value=[]):
        result = discover_all()

    assert isinstance(result, dict)
    assert "feishu" not in result


# ---------------------------------------------------------------------------
# ChannelManager integration: loud error, resilient startup
# ---------------------------------------------------------------------------


class _FakeOther(BaseChannel):
    name = "fakeother"
    display_name = "Fake Other"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, msg: OutboundMessage) -> None:
        pass


def test_manager_reports_missing_dependency_and_still_loads_other_channels():
    """Missing extra logs a loud error with hint; healthy channels still load."""
    from miniunicorn.channels.manager import ChannelManager

    fake_config = SimpleNamespace(
        channels=ChannelsConfig.model_validate(
            {
                "feishu": {"enabled": True},
                "fakeother": {"enabled": True},
            }
        ),
        providers=SimpleNamespace(groq=SimpleNamespace(api_key="", api_base="")),
    )
    dependency_error = ChannelDependencyError(
        "Channel 'feishu' is enabled in config but its dependencies are missing "
        "(No module named 'lark_oapi'). Install them with: "
        "pip install miniunicorn-ai[feishu]"
    )

    with (
        patch(
            "miniunicorn.channels.registry.discover_enabled",
            side_effect=[dependency_error, {"fakeother": _FakeOther}],
        ) as mock_discover,
        patch("miniunicorn.channels.manager.logger") as mock_logger,
    ):
        mgr = ChannelManager.__new__(ChannelManager)
        mgr.config = fake_config
        mgr.bus = MessageBus()
        mgr.channels = {}
        mgr._dispatch_task = None
        mgr._session_manager = None
        mgr._webui_static_dist = True
        mgr._init_channels()

    # First call strict (raises), second call non-strict fallback.
    assert mock_discover.call_count == 2
    assert mock_discover.call_args_list[1].kwargs.get("strict") is False

    # Startup error message contains the install hint.
    error_calls = mock_logger.error.call_args_list
    assert error_calls, "missing dependency must be logged at error level"
    joined = " ".join(str(a) for a in error_calls[0].args)
    assert "pip install miniunicorn-ai[feishu]" in joined

    # Healthy channel loaded; broken one skipped.
    assert "fakeother" in mgr.channels
    assert "feishu" not in mgr.channels
